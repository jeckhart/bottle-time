# SPDX-FileCopyrightText: 2020 Kattni Rembor, written for Adafruit Industries
#
# SPDX-License-Identifier: Unlicense
import json
import os
import rtc
import ssl
import time

import board
import neopixel

import socketpool
import wifi

import adafruit_logging as logging
import adafruit_minimqtt.adafruit_minimqtt as MQTT
import adafruit_ntp

import adafruit_datetime as adt
from adafruit_datetime import datetime
from adafruit_magtag.magtag import MagTag
from every.every import Every

from adafruit_led_animation.animation.comet import Comet
from adafruit_led_animation.animation.sparkle import Sparkle
from adafruit_led_animation.animation.chase import Chase
from adafruit_led_animation.animation.blink import Blink
from adafruit_led_animation.animation.pulse import Pulse
from adafruit_led_animation.sequence import AnimationSequence
from adafruit_led_animation.group import AnimationGroup
from adafruit_led_animation.color import RED, GREEN, BLUE, CYAN, WHITE,\
    OLD_LACE, PURPLE, MAGENTA, YELLOW, ORANGE, PINK

# =============== CUSTOMISATIONS ================
# The strip LED brightness, where 0.0 is 0% (off) and 1.0 is 100% brightness, e.g. 0.3 is 30%
strip_pixel_brightness = 0.01
# The MagTag LED brightness, where 0.0 is 0% (off) and 1.0 is 100% brightness, e.g. 0.3 is 30%.
magtag_pixel_brightness = 0.01

# ===============================================

# Set up where we'll be fetching data from
DATA_SOURCE = "http://api.thingspeak.com/channels/1417/field/1/last.json"
COLOR_LOCATION = ['field1']
DATE_LOCATION = ['created_at']

# Set your Adafruit IO Username, Key and Port in settings.toml
# (visit io.adafruit.com if you need to create an account,
# or if you need your Adafruit IO key.)
AIO_USERNAME = os.getenv("ADAFRUIT_AIO_USERNAME")
AIO_KEY = os.getenv("ADAFRUIT_AIO_KEY")
MQTT_FEED_NAME = f"{AIO_USERNAME}/feeds/{os.getenv("MQTT_FEED_NAME")}"

magtag = MagTag(
    url=DATA_SOURCE,
    json_path=(COLOR_LOCATION, DATE_LOCATION),
)
magtag.network.connect()

strip_pixels = neopixel.NeoPixel(board.D10, 30, brightness=strip_pixel_brightness)
magtag_pixels = magtag.peripherals.neopixels
magtag_pixels.brightness = magtag_pixel_brightness

# Cheerlights color
magtag.add_text(
    text_scale=2,
    text_position=(10, 15),
    text_transform=lambda x: "New Color: {}".format(x),  # pylint: disable=unnecessary-lambda
)
# datestamp
magtag.add_text(
    text_scale=2,
    text_position=(10, 65),
    text_transform=lambda x: "Updated on:\n{}".format(x),  # pylint: disable=unnecessary-lambda
)

# Create a socket pool
pool = socketpool.SocketPool(wifi.radio)
ssl_context = ssl.create_default_context()

class NTPtoRTC:

    def __init__(self, socket_pool, tz_offset=0, cache_seconds=3600, update_duration=3601, set_rtc=True):
        self.socket_pool = socket_pool
        self.tz_offset = tz_offset
        self.cache_seconds = cache_seconds
        self.update_duration = update_duration
        self.update_event = Every(self.update_duration)
        self.reconnect_event = Every(5)
        self.ntp = None
        self.connected = False
        self.set_rtc = set_rtc
        self.logger = logging.getLogger("ntp_to_rtc")

    def connect(self):
        while not self.connected:
            self.logger.debug(f"Attempting to connect to ntp")
            try:
                self.ntp = adafruit_ntp.NTP(self.socket_pool, tz_offset=self.tz_offset, cache_seconds=self.cache_seconds)
                if self.set_rtc:
                    rtc.RTC().datetime = self.ntp.datetime
                else:
                    dt = self.ntp.datetime
                self.connected = True
                self.logger.debug(f"Success connecting to ntp")
            except Exception as exp:
                self.logger.exception(exp)

    def update(self):
        if not self.connected:
            self.connect()
        try:
            self.logger.debug(f"Updating system time from ntp")
            if self.set_rtc:
                rtc.RTC().datetime = self.ntp.datetime
            else:
                dt = self.ntp.datetime
        except Exception as exp:
            self.logger.exception(exp)
            self.connected = False

    def loop(self):
        if self.reconnect_event():
            self.connect()

        if self.update_event():
            self.update()

# If you need to use certificate/key pair authentication (e.g. X.509), you can load them in the
# ssl context by uncommenting the lines below and adding the following keys to your settings.toml:
# "device_cert_path" - Path to the Device Certificate
# "device_key_path" - Path to the RSA Private Key
# ssl_context.load_cert_chain(
#     certfile=os.getenv("device_cert_path"), keyfile=os.getenv("device_key_path")
# )

class MQTTClient:
    def __init__(self, topic_name, socket_pool=None, ssl_context=None):

        self.mqtt_loop = Every(1)
        self.reconnect_event = Every(5)
        self.topic_name = topic_name
        self.handlers = []

        self.mqtt_client = MQTT.MQTT(
            broker="io.adafruit.com",
            port=1883,
            username=AIO_USERNAME,
            password=AIO_KEY,
            socket_pool=socket_pool,
            ssl_context=ssl_context,
        )
        self.logger = logging.getLogger("mqtt_client")
        self.mqtt_client.logger = self.logger

    def connect(self):
        # Set up a MiniMQTT Client
        # Setup the callback methods above
        self.mqtt_client.on_connect = self.connected
        self.mqtt_client.on_disconnect = self.disconnected
        self.mqtt_client.on_message = self.message
        self.mqtt_client.on_subscribe = self.subscribed

        # Connect the client to the MQTT broker.

        if not self.mqtt_client.is_connected():
            self.logger.debug(f"Attempting to connect to {self.mqtt_client.broker}")
            try:
                self.mqtt_client.connect()
            except Exception as exp:
                print(exp)
                time.sleep(5)

    def add_handler(self, handler, discriminator=None):
        self.logger.debug(f"Adding handler for topic {self.topic_name}")
        self.handlers.append((handler, discriminator))

    # Define callback methods which are called when events occur
    def connected(self, client, userdata, flags, rc):
        # This function will be called when the client is connected
        # successfully to the broker.
        self.logger.info(f"Connected to Adafruit IO! Listening for topic changes on {self.topic_name}")
        # Subscribe to all changes on the onoff_feed.
        client.subscribe(self.topic_name)

    def subscribed(self, client, userdata, topic, granted_qos):
        # This method is called when the client subscribes to a new feed.
        self.logger.debug(f"Subscribed to {topic} with QOS level {granted_qos}")
        self.get()


    def disconnected(self, client, userdata, rc):
        # This method is called when the client is disconnected
        self.logger.warning("Disconnected from Adafruit IO!")
        self.connected = False

    def message(self, client, topic, message):
        # This method is called when a topic the client is subscribed to
        # has a new message.
        self.logger.debug(f"New message on topic {topic}: {message}")
        for handler, discriminator in self.handlers:
            if discriminator is None or discriminator(message):
                handler(message)

    def get(self):
        """Calling this method will make Adafruit IO publish the most recent
        value on the feed.
        https://io.adafruit.com/api/docs/mqtt.html#retained-values

        Example of obtaining a recently published value on a feed:

        .. code-block:: python

            mqttclient.get()
        """
        self.mqtt_client.publish(f"{self.topic_name}/get", "\0")

    def loop(self):
        if self.reconnect_event():
            self.connect()

        if self.mqtt_loop():
            try:
                self.mqtt_client.loop()
            except Exception as exp:
                self.logger.exception(exp)

class BottleTracker:
    def __init__(self, pixels, interval=3*3600, blink_interval=1):
        self.pixels = pixels
        self.num_pixels = len(self.pixels)
        self.last_bottle_time = None
        self.interval = interval
        self.blink_interval = blink_interval
        self.next_blink_time = None
        self.blink_state = False
        self.logger = logging.getLogger("bottle_tracker")
        self.log_interval = Every(1)

    def new_bottle(self, last_bottle_time=None):
        self.last_bottle_time = last_bottle_time or adt.datetime.now()

    def update_lights(self):
        if self.last_bottle_time is None:
            return

        current_time = adt.datetime.now()
        end_time = self.last_bottle_time + adt.timedelta(seconds=self.interval)
        total_time = (end_time - self.last_bottle_time).seconds if end_time > self.last_bottle_time else 0
        time_left = (end_time - current_time).seconds if end_time > current_time else 0
        proportion_left = time_left / total_time if total_time else 0

        if self.log_interval():
            self.logger.debug(f"Current_time: {current_time} last_bottle_time: {self.last_bottle_time} end_time: {end_time} Time left: {time_left} Proportion left: {proportion_left}")

        if time_left <= 0:
            # Turn off all lights if time is up
            for i in range(self.num_pixels):
                self.pixels[i] = (0, 0, 0)
        else:
            # Calculate the proportion of time left

            num_lights_on = int(proportion_left * self.num_pixels)

            # Determine the color based on the time left
            if proportion_left < 1/18:  # Less than 10 minutes
                color = (255, 0, 0)  # Red
            elif proportion_left < 1/6:  # Less than 1 hour
                color = (255, 165, 0)  # Orange
            else:
                color = (0, 255, 0)  # Green

            # Set the lights
            for i in range(self.num_pixels):
                if i < num_lights_on:
                    self.pixels[i] = color
                else:
                    self.pixels[i] = (0, 0, 0)

            # Blink the rightmost colored pixel
            if num_lights_on > 0:
                rightmost_pixel = min(max(0, num_lights_on - 1),self.num_pixels-1)
                if not self.next_blink_time or self.next_blink_time < current_time:
                    self.next_blink_time = current_time + adt.timedelta(seconds=self.blink_interval)

                    if self.blink_state:
                        self.pixels[rightmost_pixel] = (0, 0, 0)
                    else:
                        self.pixels[rightmost_pixel] = color
                    self.blink_state = not self.blink_state

    def new_bottle_handler(self, message):
        data = json.loads(message)
        event = data.get("event")
        event_ts = data.get("event-ts")
        ounces = data.get("ounces")
        print(f"New bottle event received: {event} at {event_ts} with {ounces} ounces")
        new_bottle_ts = datetime.fromisoformat(event_ts)
        self.new_bottle(new_bottle_ts)

    @staticmethod
    def new_bottle_discriminator(message):
        data = json.loads(message)
        return data.get("event") == "new-bottle"

logging.getLogger("mqtt_client").setLevel(logging.INFO)
logging.getLogger("ntp_to_rtc").setLevel(logging.DEBUG)
logging.getLogger("bottle_tracker").setLevel(logging.INFO)

ntp_to_rtc = NTPtoRTC(pool, tz_offset=-5)
mqtt_client = MQTTClient(MQTT_FEED_NAME, pool, ssl_context)

# Example usage
bottle_tracker_magtag = BottleTracker(magtag_pixels, interval = 300)
bottle_tracker_strip = BottleTracker(strip_pixels, interval = 300)

mqtt_client.add_handler(bottle_tracker_magtag.new_bottle_handler, BottleTracker.new_bottle_discriminator)
mqtt_client.add_handler(bottle_tracker_strip.new_bottle_handler, BottleTracker.new_bottle_discriminator)

while True:
    ntp_to_rtc.loop()
    mqtt_client.loop()

    if magtag.peripherals.any_button_pressed:
        bottle_tracker_magtag.new_bottle()
        bottle_tracker_strip.new_bottle()

    bottle_tracker_magtag.update_lights()
    bottle_tracker_strip.update_lights()
    # time.sleep(1)
