# -*- coding: utf-8 -*-

import paradox_messages as msg
from serial_connection import *
import logging
import sys
import time
import json
from threading import Lock
import datetime

from config_defaults import *
from config import *

logger = logging.getLogger('PAI').getChild(__name__)

MEM_STATUS_BASE1 = 0x8000
MEM_STATUS_BASE2 = 0x1fe0

MEM_ZONE_START = 0x010
MEM_ZONE_END = MEM_ZONE_START + 0x10 * 32
MEM_OUTPUT_START = MEM_ZONE_END
MEM_OUTPUT_END = MEM_OUTPUT_START + 0x10 * 16
MEM_PARTITION_START = MEM_OUTPUT_END
MEM_PARTITION_END = MEM_PARTITION_START + 0x10 * 2
MEM_USER_START = MEM_PARTITION_END
MEM_USER_END = MEM_USER_START + 0x10 * 32
MEM_BUS_START = MEM_USER_END 
MEM_BUS_END = MEM_BUS_START + 0x10 * 15
MEM_REPEATER_START = MEM_BUS_END 
MEM_REPEATER_END = MEM_REPEATER_START + 0x10 * 2
MEM_KEYPAD_START = MEM_REPEATER_END
MEM_KEYPAD_END = MEM_KEYPAD_START + 0x10 * 8
MEM_SITE_START = MEM_KEYPAD_END
MEM_SITE_END = MEM_SITE_START + 0x10
MEM_SIREN_START = MEM_SITE_END 
MEM_SIREN_END = MEM_SIREN_START + 0x10 * 4

PARTITION_ACTIONS = dict(arm=0x04, disarm=0x05, arm_stay=0x01, arm_sleep=0x03,  arm_stay_stayd=0x06, arm_sleep_stay=0x07, disarm_all=0x08)
ZONE_ACTIONS = dict(bypass=0x10, clear_bypass=0x10)
PGM_ACTIONS = dict(on_override=0x30, off_override=0x31, on=0x32, off=0x33, pulse=0)

serial_lock = Lock()

class Paradox:
    def __init__(self,
                 connection,
                 interface,
                 retries=3):

        self.connection = connection
        self.connection.timeout(0.5)
        self.retries = retries
        self.interface = interface
        self.reset()

    def reset(self):

        # Keep track of alarm state
        self.repeaters = dict()
        self.keypads = dict()
        self.sirens = dict()
        self.sites = dict()
        self.users = dict()
        self.buses = dict()
        self.zones = dict()
        self.partitions = dict()
        self.outputs = dict()
        self.system = dict()
        self.last_power_update = 0
        self.run = False
        self.loop_wait = False
        
        self.type_to_element_dict = dict(repeater=self.repeaters, keypad=self.keypads, siren=self.sirens, user=self.users, bus=self.buses, zone=self.zones, partition=self.partitions, output=self.outputs, system=self.system)

        self.labels = {'zone': {}, 'partition': {}, 'output': {}, 'user': {}, 'bus': {}, 'repeater': {}, 'siren':{}, 'site': {}, 'keypad': {} }
        self.status_cache = dict()


    def connect(self):
        logger.info("Connecting to panel")
        
        # Reset all states
        self.reset()

        self.run = True
        
        try:
            reply = self.send_wait_for_reply(msg.InitiateCommunication, None, reply_expected=0x07)

            if reply:
                logger.info("Found Panel {} version {}.{} build {}".format(
                    (reply.fields.value.label.decode('latin').strip()),
                    reply.fields.value.application.version,
                    reply.fields.value.application.revision,
                    reply.fields.value.application.build))
            else:
                logger.warn("Unknown panel")

            reply = self.send_wait_for_reply(msg.StartCommunication, None, reply_expected=0x00)
            
            if reply is None:
                self.run = False
                return False
             
            args = dict(product_id=reply.fields.value.product_id,
                        firmware=reply.fields.value.firmware, 
                        panel_id=reply.fields.value.panel_id,
                        pc_password=PASSWORD,
                        user_code=0x00000000
                        ) 

            #reply = self.send_wait_for_reply(message=reply.fields.data + reply.checksum, raw=True, reply_expected=0x10)
            reply = self.send_wait_for_reply(msg.InitializeCommunication, args=args, reply_expected=0x10)

            if reply is None:
                self.run = False
                return False
            
            if SYNC_TIME:
                self.sync_time()

            self.update_labels()
                    
            logger.info("Connection OK")

            return True
        except:
            logger.exception("Connect error")

        self.run = False
        return False
    
    def sync_time(self):
        logger.debug("Synchronizing panel time")

        now = datetime.datetime.now()
        args = dict(century=int(now.year / 100), year=int(now.year % 100), month=now.month, day=now.day, hour=now.hour,minute=now.minute)

        reply = self.send_wait_for_reply(msg.SetTimeDate, args, reply_expected=0x03)
        if reply is None:
            logger.warn("Could not set panel time")
        
        return

    def loop(self):
        logger.debug("Loop start")
        args = {}
        
        while self.run:
            #logger.debug("Getting alarm status")
            self.loop_wait = True

            tstart = time.time()
            try:
                for i in STATUS_REQUESTS:
                    args = dict(address=MEM_STATUS_BASE1 + i)
                    reply = self.send_wait_for_reply(msg.ReadEEPROM, args, reply_expected=0x05)
                    if reply is not None:
                        self.handle_status(reply)
            except:
                logger.exception("Loop")
            
            # Listen for events
            while (time.time() - tstart) < KEEP_ALIVE_INTERVAL and self.loop_wait and self.run: 
                self.send_wait_for_reply(None, timeout=1)

    def send_wait_for_reply(self, message_type=None, args=None, message=None, retries=5, timeout=5, raw=False, reply_expected=None):
        if message is None and message_type is not None:
            message = message_type.build(dict(fields=dict(value=args)))

        while retries >= 0:
            retries -= 1

            if message is not None:
                if LOGGING_DUMP_PACKETS:
                    m = "PC -> A "
                    for c in message:
                        m += "{0:02x} ".format(c)
                    logger.debug(m)
            
            
            with serial_lock:
                if message is not None:
                    self.connection.timeout(timeout)
                    self.connection.write(message)
                data = self.connection.read()
                if raw:
                    return data

            # Retry if no data was available
            if data is None or len(data) == 0:
                if message is None:
                    return None

                time.sleep(0.25)
                continue

            if LOGGING_DUMP_PACKETS:
                m = "PC <- A "
                for c in data:
                    m += "{0:02x} ".format(c)
                logger.debug(m)

            try:
                recv_message = msg.parse(data)
            except:
                recv_message = None

            if recv_message is None:
                logging.exception("Error parsing message")
                time.sleep(0.1)
                continue

            if LOGGING_DUMP_MESSAGES:
                logger.debug(recv_message)

            # Events are async
            if recv_message.fields.value.po.command == 0xe:
                try:
                    self.handle_event(recv_message)
                except:
                    logger.exception("Handle event")

                time.sleep(0.1)
                continue
            
            if recv_message.fields.value.po.command == 0x70:
                self.handle_error(recv_message)
                return None

            if reply_expected is not None and recv_message.fields.value.po.command != reply_expected:
                logging.error("Got message {} but expected {}".format(recv_message.fields.value.po.command, reply_expected))
                logging.error("Detail:\n{}".format(recv_message))
                time.sleep(0.1)
                continue

            return recv_message

        return None

    def update_labels(self):
        logger.info("Updating Labels from Panel")
        
        output_template = dict(
            on=False,
            pulse=False)

        self.load_labels(self.zones, self.labels['zone'], MEM_ZONE_START, MEM_ZONE_END)
        logger.info("Zones: {}".format(', '.join(self.labels['zone'])))
        self.load_labels(self.outputs, self.labels['output'], MEM_OUTPUT_START, MEM_OUTPUT_END, template=output_template)
        logger.info("Outputs: {}".format(', '.join(list(self.labels['output']))))
        self.load_labels(self.partitions, self.labels['partition'], MEM_PARTITION_START, MEM_PARTITION_END)
        logger.info("Partitions: {}".format(', '.join(list(self.labels['partition']))))
        self.load_labels(self.users, self.labels['user'], MEM_USER_START, MEM_USER_END)
        logger.info("Users: {}".format(', '.join(list(self.labels['user']))))
        self.load_labels(self.buses, self.labels['bus'], MEM_BUS_START, MEM_BUS_END)
        logger.info("Buses: {}".format(', '.join(list(self.labels['bus']))))
        self.load_labels(self.repeaters, self.labels['repeater'], MEM_REPEATER_START, MEM_REPEATER_END)
        logger.info("Repeaters: {}".format(', '.join(list(self.labels['repeater']))))
        self.load_labels(self.keypads, self.labels['keypad'], MEM_KEYPAD_START, MEM_KEYPAD_END)
        logger.info("Keypads: {}".format(', '.join(list(self.labels['keypad']))))
        self.load_labels(self.sites, self.labels['site'], MEM_SITE_START, MEM_SITE_END)
        logger.info("Sites: {}".format(', '.join(list(self.labels['site']))))
        self.load_labels(self.sirens, self.labels['siren'], MEM_SIREN_START, MEM_SIREN_END)
        logger.info("Sirens: {}".format(', '.join(list(self.labels['siren']))))

        logger.debug("Labels updated")
        
    def load_labels(self,
                    labelDictIndex,
                    labelDictName,
                    start,
                    end,
                    limit=range(1, 33),
                    template=dict(label='')):
        """Load labels from panel"""
        i = 1
        address = start

        if len(limit) == 0:
            return
        
        while address < end and i <= max(limit):
            args = dict(address=address)
            reply = self.send_wait_for_reply(msg.ReadEEPROM, args, reply_expected=0x05)
            
            if reply is None:
                logger.error("Could not fully load labels")
                return
           
            # Avoid errors due to colision with events
            if reply.fields.value.address != address:
                continue

            payload = reply.fields.value.data
            label = payload[:16].strip().decode('latin').replace(" ","_")
                
            if label not in labelDictName and i in limit:
                properties = template.copy()
                properties['label'] = label
                labelDictIndex[i] = properties

                labelDictName[label] = i
            i += 1

            address += 16

    def control_zone(self, zone, command):
        logger.debug("Control Zone: {} - {}".format(zone, command))

        if command not in self.ZONE_COMMANDS:
            return False

        zones_selected = []
        # if all or 0, select all
        if zone == 'all' or zone == '0':
            zones_selected = list(self.zones)
        else:
            # if set by name, look for it
            if zone in self.labels['zone']:
                zones_selected = [self.labels['zone'][zone]]
            # if set by number, look for it
            elif zone.isdigit():
                number = int(zone)
                if number in self.zones:
                    zones_selected = [number]

        # Not Found
        if len(zones_selected) == 0:
            return False

        # Apply state changes
        accepted = False
        for e in zones_selected:
            args = dict(action=self.ZONES[command], argument=e)
            reply = self.send_wait_for_reply(msg.PerformAction, args, reply_expected=0x04)
            
            if reply is not None:
                accepted = True

        # Refresh status
        self.loop_wait = False
        return accepted

    def control_partition(self, partition, command):
        logger.debug("Control Partition: {} - {}".format(partition, command))
        
        if command not in PARTITION_ACTIONS:
            return False

        partitions_selected = []
        # if all or 0, select all
        if partition == 'all' or partition == '0':
            partitions_selected = list(self.partitions)
        else:
            # if set by name, look for it
            if partition in self.labels['partition']:
                partitions_selected = [self.labels['partition'][partition]]
            # if set by number, look for it
            elif partition.isdigit():
                number = int(partition)
                if number in self.partitions:
                    partitions_selected = [number]

        # Not Found
        if len(partitions_selected) == 0:
            return False

        # Apply state changes
        accepted = False
        for e in partitions_selected:
            args = dict(action=PARTITION_ACTIONS[command], argument=e)
            reply = self.send_wait_for_reply(msg.PerformAction, args, reply_expected=0x04)

            if reply is not None:
                accepted = True

        # Refresh status
        self.loop_wait = False
        
        return accepted

    def control_output(self, output, command):
        logger.debug("Control Partition: {} - {}".format(output, command))

        if command not in PGM_ACTIONS:
            return False

        outputs = []
        # if all or 0, select all
        if output == 'all' or output == '0':
            outputs = list(range(1, len(self.outputs)))
        else:
            # if set by name, look for it
            if output in self.labels['output']:
                outputs = [self.labels['output'][output]]
            # if set by number, look for it
            elif output.isdigit():
                number = int(output)
                if number > 0 and number < len(self.outputs):
                    outputs = [number]

        # Not Found
        if len(outputs) == 0:
            return False
        
        accepted = False

        for e in outputs:
            if command == 'pulse':
                args = dict(action=PGM_COMMAND['on'], argument=e)
                reply = self.send_wait_for_reply(msg.PerformAction, args, reply_expected=0x04)
                if reply is not None:
                    accepted = True

                time.sleep(1)
                args = dict(action=PGM_COMMAND['off'], argument=e)
                reply = self.send_wait_for_reply(msg.PerformAction, args, reply_expected=0x04)
                if reply is not None:
                    accepted = True
            else:
                args = dict(action=PGM_COMMAND[command], argument=e)
                reply = self.send_wait_for_reply(msg.PerformAction, args, reply_expected=0x04)
                if reply is not None:
                    accepted = True

        # Refresh status
        self.loop_wait = False
        
        return accepted

    def handle_event(self, message):
        """Process Live Event Message and dispatch it to the interface module"""
        event = message.fields.value.event
        logger.debug("Handle Event: {}".format(event))
        
        new_event = self.process_event(event)
        
        self.generate_event_notifications(new_event)

        # Publish event
        if self.interface is not None:
            self.interface.event(raw=new_event)
        

    
    def generate_event_notifications(self, event):
        major_code = event['major'][0]
        minor_code = event['minor'][0]

        # IGNORED
        
        # Clock loss
        if major_code == 45 and minor_code == 6:
            return

        # Open Close
        if major_code in [0, 1]:
            return

        # Squawk on off, Partition Arm Disarm
        if major_code == 2 and minor_code in [8, 9, 11, 12, 14]:
            return

        # Bell Squawk
        if major_code == 3 and minor_code in [2, 3]:
            return

        # Arm in Sleep
        if major_code == 6 and minor_code in [3, 4]:
            return

        # Arming Through Winload
        # Partial Arming
        if major_code == 30 and minor_code in [3, 5]:
            return

        # Disarming Through Winload
        if major_code == 34 and minor_code == 1:
            return
        
        # Software Log on
        if major_code == 48 and minor_code == 2:
            return

        
        ## CRITICAL Events

        # Fire Delay Started
        # Zone in Alarm
        # Fire Alarm
        # Zone Alarm Restore
        # Fire Alarm Restore
        # Zone Tampered
        # Zone Tamper Restore
        # Non Medical Alarm
        if major_code in [24, 36, 37, 38, 39, 40, 42, 43, 57] or \
            ( major_code in [44, 45] and minor_code in [1, 2, 3, 4, 5, 6, 7]):
            # Zone Alarm Restore
            #if major_code in [36, 38]:
            
                #detail = self.zones[event['minor'][0]]['label']
            #else:
            detail = event['minor'][1]

            self.interface.notify("Paradox", "{} {}".format(event['major'][1], detail), logging.CRITICAL)
        
        # Silent Alarm
        # Buzzer Alarm
        # Steady Alarm
        # Pulse Alarm
        # Strobe
        # Alarm Stopped
        # Entry Delay
        elif major_code == 2:
            if minor_code in [2, 3, 4, 5, 6, 7, 13]:
                self.interface.notify("Paradox", event['minor'][1], logging.CRITICAL)
                
            elif minor_code == 13:
                self.interface.notify("Paradox", event['minor'][1], logging.INFO)

        # Special Alarm, New Trouble and Trouble Restore
        elif major_code in [40, 44, 45] and minor_code in [1, 2, 3, 4, 5, 6, 7]:
            self.interface.notify("Paradox", "{}: {}".format(event['major'][1], event['minor'][1]), logging.CRITICAL)
        # Signal Weak
        elif major_code in [18, 19, 20, 21]:
            if event['minor'][0] >= 0 and event['minor'][0] < len(self.zones):
                label = self.zones[event['minor'][0]]['label']
            else:
                label = event['minor'][1]
            
            self.interface.notify("Paradox", "{}: {}".format(event['major'][1], label), logging.INFO)
        else:
            # Remaining events trigger lower level notifications
            self.interface.notify("Paradox", "{}: {}".format(event['major'][1], event['minor'][1]), logging.INFO)


    def process_event(self, event):

        major = event['major'][0]
        minor = event['minor'][0]

        change = None

        # ZONES
        if major in (0, 1):
            change=dict(open=(major==1))
        elif major == 35:
            change=dict(bypass=not self.zones[minor])
        elif major in (36, 38):
            change=dict(alarm=(major==36))
        elif major in (37, 39):
            change=dict(fire_alarm=(major==37))
        elif major == 41:
            change=dict(shutdown=True)
        elif major in (42, 43):
            change=dict(tamper=(major==42))
        elif major in (49, 50):
            change=dict(low_battery=(major==49))
        elif major in (51, 52):
            change=dict(supervision_trouble=(major==51))
        
        # PARTITIONS
        elif major == 2:
            if minor in (2, 3, 4, 5, 6):
                change=dict(alarm=True)
            elif minor == 7:
                change = dict(alarm=False)
            elif minor == 11:
                change = dict(arm=False, arm_full=False, arm_sleep=False, arm_stay=False, alarm=False)
            elif minor == 12:
                change = dict(arm=True)
            elif minor == 14:
                change = dict(exit_delay=True)
        elif major == 3:
            if minor in (0, 1):
                change=dict(bell=(minor==1))
        elif major == 6:
            if minor == 3:
                change = dict(arm=True, arm_full=False, arm_sleep=False, arm_stay=True, alarm=False)
            elif minor == 4:
                change = dict(arm=True, arm_full=False, arm_sleep=True, arm_stay=False, alarm=False)  
        # Wireless module
        elif major in (53, 54):
            change = dict(supervision_trouble=(major==53))
        elif major in (53, 56):
            change = dict(tamper_trouble=(major==55))

        new_event = {'major': event['major'], 'minor': event['minor'], 'type': event['type'] }
        
        if change is not None:
            if event['type'] == 'Zone' and len(self.zones) > 0 and minor < len(self.zones):
                self.update_properties('zone',  minor, change)
                new_event['minor'] = (minor, self.zones[minor]['label'])
            elif event['type'] == 'Partition' and len(self.partitions) > 0:
                pass
                #self.update_properties('partition', self.partitions, minor, change)
                #new_event['minor'] = (minor, self.partitions[minor]['label'])
            elif event['type'] == 'Output' and len(self.outputs) and minor < len(self.outputs):
                self.update_properties('output',  minor, change)
                new_event['minor'] = (minor, self.outputs[minor]['label'])

        return new_event


    def update_properties(self, element_type, key, change):

        elements = self.type_to_element_dict[element_type]

        #logger.debug("Update Properties {} {} {}".format(element_type, index, change))
        if key not in elements:
            #logger.debug("Key {} not in elements {}".format(key, element_type))            
            return

        # Publish changes and update state
        for k, v in change.items():
            old = None
            if k in elements[key]:
                old = elements[key][k]
        
                if old != change[k]:
                    logger.debug("Change {}/{}/{} from {} to {}".format(element_type, elements[key]['label'], k, old, change[k]))
                    elements[key][k] = change[k]
                    self.interface.change(element_type, elements[key]['label'],
                                          k, change[k], initial=False)

                    # Trigger notifications for Partitions changes
                    # Ignore some changes as defined in the configuration
                    if element_type == "partition" and k in PARTITIONS:
                        self.interface.notify("Paradox", "{} {} {}".format(elements[key]['label'], k, change[k]), logging.INFO)

            else:
                elements[key][k] = v # Initial value
                surpress = 'trouble' not in k

                self.interface.change(element_type, elements[key]['label'],
                                          k, change[k], initial=surpress)

    def process_status_bulk(self, message):

        for k in message.fields.value:
            element_type = k.split('_')[0]
            
            if element_type == 'pgm':
                element_type = 'output'
                limit_list = OUTPUTS
            elif element_type == 'zone':
                limit_list = ZONES
            elif element_type == 'bus':
                limit_list = BUSES
            elif element_type == 'wireless-repeater':
                element_type = 'repeater'
                limit_list  == REPEATERS
            elif element_type == 'wireless-keypad':
                element_type = 'keypad'
                limit_list  == KEYPADS
            else:
                continue
            
            if k in self.status_cache and self.status_cache[k] == message.fields.value[k]:
                continue
            
            self.status_cache[k] = message.fields.value[k]
            
            prop_name = '_'.join(k.split('_')[1:])
            if prop_name == 'status':
                for kk in message.fields.value[k]:
                    i = 1
                    while i in limit_list and i in message.fields.value[k][kk]:
                        status = message.fields.value[k][kk][i]
                        self.update_properties(element_type, i, {prop_name:status})
                        i += 1
            else:
                i = 1
                while i in limit_list and i in message.fields.value[k]:
                    status = message.fields.value[k][i]
                    self.update_properties(element_type, i, {prop_name:status})
                    i += 1


    def handle_status(self, message):
        """Handle MessageStatus"""
        
        if message.fields.value.status_request == 0:
            if time.time() - self.last_power_update >= POWER_UPDATE_INTERVAL:
                self.last_power_update = time.time()
                self.update_properties('system', 'power', dict(vdc=round(message.fields.value.vdc, 2)))
                self.update_properties('system', 'power', dict(battery=round(message.fields.value.battery, 2)))
                self.update_properties('system', 'power', dict(dc=round(message.fields.value.dc, 2)))
            
            self.update_properties('system','rf', dict(rf_noise_floor=round(message.fields.value.rf_noise_floor, 2 )))
            for k in message.fields.value.troubles:
                if "not_used" in k:
                    continue

                self.update_properties('system', 'trouble', {k: message.fields.value.troubles[k]})
           
            self.process_status_bulk(message)
        
        elif message.fields.value.status_request == 1:
            self.process_status_bulk(message)
        
        elif message.fields.value.status_request == 2:
            self.process_status_bulk(message)
        
        elif message.fields.value.status_request == 3:
            self.process_status_bulk(message)
        
        elif message.fields.value.status_request == 4:
            self.process_status_bulk(message)
        
        elif message.fields.value.status_request == 5:
            self.process_status_bulk(message)

    def handle_error(self, message):
        """Handle ErrorMessage"""
        logger.warn("Got ERROR Message: {}".format(message.fields.value.message))
        self.run = False

    def disconnect(self):
        logger.info("Disconnecting from the Alarm Panel")
        self.run = False
        self.loop_wait = False
        reply = self.send_wait_for_reply(msg.CloseConnection, None, reply_expected=0x07)
        
