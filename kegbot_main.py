#!/usr/bin/env python2

# Used in scheduling
import time
from datetime import datetime, timedelta

# System modules
import os
from sys import platform as _platform

# For sending email
from smtplib import SMTP
from email.mime.text import MIMEText
import string

# For Twitter interface
from threading import Thread
from collections import deque
from requests.exceptions import ChunkedEncodingError
from twython import Twython, TwythonStreamer

# For parsing YAML files
import yaml

# Receiving sensor data from Arduino
import serial

# Scheduling tasks like sending status email
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
import logging
logging.basicConfig(filename='reducedKegbot.log',
                    filemode='a',
                    format='[%(asctime)s] %(message)s',
                    datefmt='%Y/%d/%m %H:%M:%S',
                    level=logging.INFO)

# Regex for extracting data from Twitter control messages 
import re

# Pushbullet Interface
from pushbullet import Pushbullet

# Import JSON for communicating with website
import json

#####################################################
# Setup
#####################################################

# Project Path (on the Pi)
config_path = "/home/pi/prj/reducedKegbot"
os.chdir(config_path)

# Import secret configuration info from secret.yaml
secret_config_yaml = open(config_path+"/secret.yaml", 'r')
secret_config = yaml.load(secret_config_yaml)
secret_config_yaml.close()

# EMAIL
KEGBOT_ADMINS = secret_config['KEGBOT_ADMINS']
kb_email_addr = secret_config['kegbot_email_addr']
smtp_server = secret_config['smtp_server']

# Twitter info
APP_KEY = secret_config['APP_KEY']
APP_SECRET = secret_config['APP_SECRET']
OAUTH_TOKEN = secret_config['OAUTH_TOKEN']
OAUTH_TOKEN_SECRET = secret_config['OAUTH_TOKEN_SECRET']
TWITTER_SEARCH_TERM = secret_config['TWITTER_SEARCH_TERM']
TWITTER_REGEX = r"#NUVATION_KEGBOT (\d):(\d+\.\d*):([a-zA-Z]+):(\d+/\d+/\d+):(.*):(.*)"
APPROVED_TWITTER_ADMINS = secret_config['APPROVED_TWITTER_ADMINS']
# Make a twitter object for SENDING tweets
twitter = Twython(APP_KEY, APP_SECRET, OAUTH_TOKEN, OAUTH_TOKEN_SECRET)

# Pushbullet
PUSHBULLET_ACCESS_TOKEN = secret_config['PUSHBULLET_ACCESS_TOKEN']

# Import keg/tap info from taps.yaml
taps_yaml = open(config_path+"/taps.yaml", 'r')
taps = yaml.load(taps_yaml)
taps_yaml.close()
# Immediately update the JSON file for the website
with open('/var/www/html/taps.json', 'w') as jsonfile:
    json.dump(taps, jsonfile, indent=4)
    jsonfile.close()
    
# Import configuration from kegbot_config.yaml
kegbot_config_yaml = open(config_path+"/kegbot_config.yaml", 'r')
kb_config = yaml.load(kegbot_config_yaml)
kegbot_config_yaml.close()

# Flow meter pulse counts to volume conversion
cts_per_oz = kb_config['CTS_PER_OZ'] # ~170.5 pulses/oz.

# set greater than 0 to make things print out development messages
verbosity = kb_config['VERBOSITY']

# Send low keg warning when volume reaches this level
low_vol_thresh = kb_config['LOW_VOL_THRESH']

# How long to wait after no flow activity before updating taps.yaml
yaml_wait_time = kb_config['YAML_WAIT_TIME']

# Regex for parsing flow data from Arduino
FLOW_REGEX = r"tap1:(\d+) tap2:(\d+) tap3:(\d+)"
# Serial communication with sensor interface (Arduino)
serial_baud = kb_config['SERIAL_BAUD']
if _platform == "linux" or _platform == "linux2": # linux
    serial_port = kb_config['SERIAL_PORT_LINUX']
elif _platform == "win32": # Windows...
    serial_port = kb_config['SERIAL_PORT_WINDOWS']

# Declare Variables
email_subject = ""
email_body = ""
tap_flow_counts = [0, 0, 0]


#####################################################
# Support Functions
#####################################################

def send_status_email(): #(taps):
    # Generate subject line
    email_subject = generate_email_subject(taps)
    # Generate status email message body
    email_body = generate_email_body(taps)
    # send email update with volumes    
    send_email(kb_email_addr, KEGBOT_ADMINS, email_subject, email_body, smtp_server)
    print("Sent status email")

def generate_email_subject(taps_dict):
    # subject = "TESTSUBJECT, time is: %s" % datetime.now()
    subject = "KEGBOT STATUS"
    # Check if any taps should generate alert
    # print("Looking for kegs under %.2f" % low_vol_thresh)
    for tap in taps_dict:
        if taps_dict[tap][2] == "ACTIVE" and taps_dict[tap][0] < low_vol_thresh:
            subject = "- ALERT - %.2f GAL REMAINING ON TAP %d" % (taps_dict[tap][0], tap)
            break
    return subject
    
def generate_email_body(taps_dict):
    '''
    Should use an enumerate() loop to iterate over the number of taps, for when there aren't 3 taps.
    
    '''
    body = '''
Tap 1, %s, %.2fgal (%dL) remaining\n
Tap 2, %s, %.2fgal (%dL) remaining\n
Tap 3, %s, %.2fgal (%dL) remaining''' % (taps_dict[1][4], taps_dict[1][0],
                                        (3.78541178*taps_dict[1][0]),
                                        taps_dict[2][4], taps_dict[2][0],
                                        (3.78541178*taps_dict[2][0]),
                                        taps_dict[3][4], taps_dict[3][0],
                                        (3.78541178*taps_dict[3][0]))
    return body

def send_email(from_email, to_email, msg_subj, msg_body, mail_server):
    '''
    Sends an email to recipients(to_email), from the email address specified in
    argument from_email, with the given subject and body. Use the mail server
    specified in argument mail_server
    
    All arguments are given as strings
    '''
    BODY = string.join((
        "From: %s" % from_email,
        "To: %s" % to_email,
        "Subject: %s" % msg_subj ,
        "Body: %s" % msg_body
        ), "\r\n")
    s = SMTP(mail_server)
    s.sendmail(from_email, to_email, BODY)
    s.quit()

def convert_to_volume(flow_counts):
    '''
    Convert the number of pulses, sent by the arduino for each tap, to volume
    of beer poured. Use floats and ounces.
    '''
    # Determine volume poured on each tap
    for ii in range(len(flow_counts)):
        # Convert the counts to gallons poured for this tap
        ounces_poured = flow_counts[ii] / cts_per_oz
        gallons_poured = ounces_poured / 128
        # Update current gallons remaining for this keg
        taps[ii+1][0] = taps[ii+1][0] - gallons_poured
    print("New volumes: tap1: {0:.2f}gal, tap2: {1:.2f}gal, tap3: {2:.2f}gal".format(taps[1][0], taps[2][0], taps[3][0]))

    
def update_taps_yaml(taps_dict):#tap, starting_vol, is_active, date_tapped, long_name, short_name):
    '''
    Updates the taps.yaml file, which stores beer data
    '''
    with open('taps.yaml', 'w') as outfile:
        outfile.write(yaml.dump(taps_dict, default_flow_style=False))
        outfile.close()

def update_taps_json(taps_dict):#tap, starting_vol, is_active, date_tapped, long_name, short_name):
    '''
    Updates the json file, which is used by the website to display beer data
    '''
    with open('/var/www/html/taps.json', 'w') as jsonfile:
        json.dump(taps_dict, jsonfile, indent=4)
        jsonfile.close()
        
def update_keg_data(taps_dict):
    '''
    Calls functions to update the
    '''
    update_taps_yaml(taps_dict)
    update_taps_json(taps_dict)


def update_taps_dict(tap, starting_vol, is_active, date_tapped, long_name, short_name):
    '''
    Given a set of parameters for a single new keg, this function updates
    the taps.yaml to reflect that new keg
    '''
    # Edit the taps dictionary
    taps[tap][0] = starting_vol
    taps[tap][1] = starting_vol
    taps[tap][2] = is_active
    taps[tap][3] = date_tapped
    taps[tap][4] = long_name
    taps[tap][5] = short_name

        
def pushbullet_new_keg_update(taps, pb_object):
    pb_title = "New Keg!"
    pb_msg = "Tap 1: %s (%.2f gal.)\nTap 2: %s (%.2f gal.)\nTap 3: %s (%.2f gal.)" % (taps[1][5], taps[1][0], taps[2][5], taps[2][0], taps[3][5], taps[3][0])
    print("Sent New-Keg Push!")
    push = pb_object.push_note(pb_title, pb_msg)

def tweet_new_keg_update(taps, new_keg_tap_num):
    # TWITTER
    message = "#NuvationHasANewKeg\nTap 1: %s (%.2f gal.)\nTap 2: %s (%.2f gal.)\nTap 3: %s (%.2f gal.)" % (taps[1][5], taps[1][0], taps[2][5], taps[2][0], taps[3][5], taps[3][0])
    try:
        twitter.update_status(status=message)
        print("New-keg Tweet sent!")
    except TwythonError as e:
        print e

class TwitterStream(TwythonStreamer):
    def __init__(self, consumer_key, consumer_secret, token, token_secret, tqueue):
        self.tweet_queue = tqueue
        super(TwitterStream, self).__init__(consumer_key, consumer_secret, token, token_secret)

    def on_success(self, data):
        if 'text' in data:
            self.tweet_queue.append(data)

    def on_error(self, status_code, data):
        print(status_code)
        # Want to stop trying to get data because of the error?
        # Uncomment the next line!
        # self.disconnect()

def stream_tweets(tweets_queue):
    try:
        stream = TwitterStream(APP_KEY, APP_SECRET, OAUTH_TOKEN, OAUTH_TOKEN_SECRET, tweets_queue)
        stream.statuses.filter(track=TWITTER_SEARCH_TERM)#, language='en')
    except ChunkedEncodingError:
        # Sometimes the API sends back one byte less than expected which results in an exception in the
        # current version of the requests library
        stream_tweets(tweet_queue)

def tweet_checker(tweets_queue):
    new_tweet = tweets_queue.popleft()
    if 'text' in new_tweet:
        check_tweet = new_tweet['text'].encode('utf-8')
        print("converted tweet to text")
        # Check whether the expression matches the date string
        if re.search(TWITTER_REGEX, check_tweet):
            print("Tweet matched regex")
            if new_tweet['user']['screen_name'].lower() in APPROVED_TWITTER_ADMINS:
                print("Tweet came from an approved Kegbot admin")
                m = re.search(TWITTER_REGEX, check_tweet)
                tap = int(m.group(1)) 
                starting_vol = float(m.group(2))
                is_active = m.group(3) # string
                date_tapped = m.group(4) # string
                long_name = m.group(5) # string
                short_name = m.group(6) # string
                print("Putting %.2f gal. (%.1fL) of %s on Tap #%d" % (starting_vol, starting_vol*3.785, long_name, tap))
                update_taps_dict(tap, starting_vol, is_active, date_tapped, long_name, short_name)
                update_taps_yaml(taps)
                update_taps_json(taps)
                tweet_new_keg_update(taps, tap)
                pushbullet_new_keg_update(taps, pb)
                # etc.
            # Correct Regex, but from UNAPPROVED admin. They must be abused.
            else:
                print("Tweet not from approved admin")
                #abuse_hacker(data)
        else:
            print("Tweet doesn't match regular expression")
    else:
        print("Tweet does not contain text")

#####################################################
# Program Setup
#####################################################

# Configure serial interface to Arduino (for sensors)
try:
    ser = serial.Serial(serial_port, serial_baud)#, timeout=1)
    if ser.isOpen():
        time.sleep(1)
        ser.flushInput()
        print(ser.name + ' is flushed and open at {}bd'.format(serial_baud))
except:
    print("Error opening serial connection")

# Make a Pushbullet object
try:
    pb = Pushbullet(PUSHBULLET_ACCESS_TOKEN)
    print("Configured pushbullet...")
except:
    print("Could not start pushbullet interface")


#####################################################
# Main Program Loop
#####################################################

if __name__ == '__main__':
    sched = BackgroundScheduler()
    sched.add_job(send_status_email, 'cron', hour='16') #GMT??? How to set timezone?
    sched.start()
    # Create Tweets Queue
    tweet_queue = deque()
    # Create twitter search thread and start the stream search
    tweet_stream = Thread(target=stream_tweets, args=(tweet_queue,))
    tweet_stream.daemon = True
    tweet_stream.start()
    # Update taps.json to ensure website is accurate
    update_taps_json(taps)
    try:
        print("Waiting for flow or Tweets...")
        while True:
        ########### Check Flow Meters ##############
            # Read flow data from serial interface
            flow_input = ser.readline().strip()
            # Use regex search to extract flow count for each tap
            print(flow_input)
            if re.search(FLOW_REGEX, flow_input):
                flow_data = re.search(FLOW_REGEX, flow_input)
                # Save flow data to a list
                tap_flow_counts = [int(flow_data.group(1)), int(flow_data.group(2)), int(flow_data.group(3))]
                if tap_flow_counts[0] > 0 or tap_flow_counts[1] > 0 or tap_flow_counts[2] > 0:
                    # Convert tap_flow_counts[] to volume
                    print(tap_flow_counts)
                    convert_to_volume(tap_flow_counts)
                    # Update taps.yaml and taps.json
                    update_taps_yaml(taps)
                    update_taps_json(taps)
            else:
                print("Regex problem")
                
        ########### Check tweet queue ##############
            if len(tweet_queue) > 0:
                print("Tweet queue has {0} tweet(s)".format(len(tweet_queue)))
                # Send tweet to tweet_checker
                tweet_checker(tweet_queue)
                

                
                
    except (KeyboardInterrupt, SystemExit):
        # Stop Cron Scheduler
        print("Shutting down cron scheduler...")
        sched.shutdown()
        
        # Close Serial
        print("Closing serial connection...")
        ser.close()
        
