# Copyright 2015 Spotify AB. All rights reserved.
#
# The contents of this file are licensed under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

import pyeapi
import re

from base import NetworkDriver

from exceptions import MergeConfigException, ReplaceConfigException, SessionLockedException

from datetime import datetime
import time

from utils import string_parsers


class EOSDriver(NetworkDriver):

    def __init__(self, hostname, username, password, timeout=60):
        self.device = None
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        self.config_session = None

    def open(self):
        connection = pyeapi.client.connect(
            transport='https',
            host=self.hostname,
            username=self.username,
            password=self.password,
            port=443
        )
        self.device = pyeapi.client.Node(connection)

    def close(self):
        self.discard_config()

    def _load_config(self, filename=None, config=None, replace=True):
        if self.config_session is not None:
            raise SessionLockedException('Session is already in use by napalm')
        else:
            self.config_session = 'napalm_{}'.format(datetime.now().microsecond)

        commands = list()
        commands.append('configure session {}'.format(self.config_session))

        if replace:
            commands.append('rollback clean-config')

        if filename is not None:
            with open(filename, 'r') as f:
                lines = f.readlines()
        else:
            if isinstance(config, list):
                lines = config
            else:
                lines = config.splitlines()

        for line in lines:
            line = line.strip()
            if line == '':
                continue
            if line.startswith('!'):
                continue
            commands.append(line)

        try:
            self.device.run_commands(commands)
        except pyeapi.eapilib.CommandError as e:
            self.discard_config()

            if replace:
                raise ReplaceConfigException(e.message)
            else:
                raise MergeConfigException(e.message)

    def load_replace_candidate(self, filename=None, config=None):
        self._load_config(filename, config, True)

    def load_merge_candidate(self, filename=None, config=None):
        self._load_config(filename, config, False)

    def compare_config(self):
        if self.config_session is None:
            return ''
        else:
            commands = ['show session-config named %s diffs' % self.config_session]
            result = self.device.run_commands(commands, encoding='text')[0]['output']

            result = '\n'.join(result.splitlines()[2:])

            return result.strip()

    def commit_config(self):
        commands = list()
        commands.append('copy startup-config flash:rollback-0')
        commands.append('configure session {}'.format(self.config_session))
        commands.append('commit')
        commands.append('write memory')

        self.device.run_commands(commands)
        self.config_session = None

    def discard_config(self):
        if self.config_session is not None:
            commands = list()
            commands.append('configure session {}'.format(self.config_session))
            commands.append('abort')
            self.device.run_commands(commands)
            self.config_session = None

    def rollback(self):
        commands = list()
        commands.append('configure replace flash:rollback-0')
        commands.append('write memory')
        self.device.run_commands(commands)

    def get_facts(self):
        commands = list()
        commands.append('show version')
        commands.append('show hostname')
        commands.append('show interfaces status')

        result = self.device.run_commands(commands)

        version = result[0]
        hostname = result[1]
        interfaces_dict = result[2]['interfaceStatuses']

        uptime = time.time() - version['bootupTimestamp']

        interfaces = [i for i in interfaces_dict.keys() if '.' not in i]
        interfaces = string_parsers.sorted_nicely(interfaces)

        return {
            'hostname': hostname['hostname'],
            'fqdn': hostname['fqdn'],
            'vendor': u'Arista',
            'model': version['modelName'],
            'serial_number': version['serialNumber'],
            'os_version': version['internalVersion'],
            'uptime': int(uptime),
            'interface_list': interfaces,
        }

    def get_interfaces(self):
        commands = list()
        commands.append('show interfaces')
        output = self.device.run_commands(commands)[0]

        interfaces = dict()

        for interface, values in output['interfaces'].iteritems():
            interfaces[interface] = dict()

            if values['lineProtocolStatus'] == 'up':
                interfaces[interface]['is_up'] = True
                interfaces[interface]['is_enabled'] = True
            else:
                interfaces[interface]['is_up'] = False
                if values['interfaceStatus'] == 'disabled':
                    interfaces[interface]['is_enabled'] = False
                else:
                    interfaces[interface]['is_enabled'] = True

            interfaces[interface]['description'] = values['description']

            interfaces[interface]['last_flapped'] = values.pop('lastStatusChangeTimestamp', None)

            interfaces[interface]['speed'] = values['bandwidth']
            interfaces[interface]['mac_address'] = values.pop('physicalAddress', u'')

        return interfaces

    def get_lldp_neighbors(self):
        commands = list()
        commands.append('show lldp neighbors')
        output = self.device.run_commands(commands)[0]['lldpNeighbors']

        lldp = dict()

        for n in output:
            if n['port'] not in lldp.keys():
                lldp[n['port']] = list()

            lldp[n['port']].append(
                {
                    'hostname': n['neighborDevice'],
                    'port': n['neighborPort'],
                }
            )

        return lldp

    def get_interfaces_counters(self):
        commands = list()

        commands.append('show interfaces counters')
        commands.append('show interfaces counters errors')

        output = self.device.run_commands(commands)

        interface_counters = dict()

        for interface, counters in output[0]['interfaces'].iteritems():
            interface_counters[interface] = dict()

            interface_counters[interface]['tx_octets'] = counters['outOctets']
            interface_counters[interface]['rx_octets'] = counters['inOctets']
            interface_counters[interface]['tx_unicast_packets'] = counters['outUcastPkts']
            interface_counters[interface]['rx_unicast_packets'] = counters['inUcastPkts']
            interface_counters[interface]['tx_multicast_packets'] = counters['outMulticastPkts']
            interface_counters[interface]['rx_multicast_packets'] = counters['inMulticastPkts']
            interface_counters[interface]['tx_broadcast_packets'] = counters['outBroadcastPkts']
            interface_counters[interface]['rx_broadcast_packets'] = counters['inBroadcastPkts']
            interface_counters[interface]['tx_discards'] = counters['outDiscards']
            interface_counters[interface]['rx_discards'] = counters['inDiscards']

            # Errors come from a different command
            errors = output[1]['interfaceErrorCounters'][interface]
            interface_counters[interface]['tx_errors'] = errors['outErrors']
            interface_counters[interface]['rx_errors'] = errors['inErrors']


        return interface_counters



    # def get_bgp_neighbors(self):
    #     bgp_neighbors = dict()
    #
    #     for vrf, vrf_data in self.device.show_ip_bgp_summary_vrf_all()['vrfs'].iteritems():
    #         bgp_neighbors[vrf] = dict()
    #         bgp_neighbors[vrf]['router_id'] = vrf_data['routerId']
    #         bgp_neighbors[vrf]['local_as'] = vrf_data['asn']
    #         bgp_neighbors[vrf]['peers'] = dict()
    #
    #         for n, n_data in vrf_data['peers'].iteritems():
    #             bgp_neighbors[vrf]['peers'][n] = dict()
    #
    #             if n_data['peerState'] == 'Established':
    #                 bgp_neighbors[vrf]['peers'][n]['is_up'] = True
    #                 bgp_neighbors[vrf]['peers'][n]['is_enabled'] = True
    #             else:
    #                 bgp_neighbors[vrf]['peers'][n]['is_up'] = False
    #
    #                 reason = n_data.pop('peerStateIdleReason', None)
    #                 if reason == 'Admin':
    #                     bgp_neighbors[vrf]['peers'][n]['is_enabled'] = False
    #                 else:
    #                     bgp_neighbors[vrf]['peers'][n]['is_enabled'] = True
    #
    #             bgp_neighbors[vrf]['peers'][n]['remote_as'] = n_data['asn']
    #             bgp_neighbors[vrf]['peers'][n]['uptime'] = n_data['upDownTime']
    #
    #             raw_data = eval(
    #                 'self.device.show_ip_bgp_neighbors_vrf_{}(format="text", pipe="section {}")'.format(vrf, n)
    #             )['output']
    #
    #             n_data_full =  colon_separated_string_to_dict(raw_data)
    #             sent, rcvd = n_data_full['IPv4 Unicast'].split()
    #
    #             bgp_neighbors[vrf]['peers'][n]['received_prefixes'] = int(rcvd)
    #             bgp_neighbors[vrf]['peers'][n]['sent_prefixes'] = int(sent)
    #             bgp_neighbors[vrf]['peers'][n]['accepted_prefixes'] = n_data['prefixAccepted']
    #
    #             bgp_neighbors[vrf]['peers'][n]['description'] = n_data_full.pop('Description', '')
    #
    #     return bgp_neighbors

    def get_environment(self):
        """
        Returns a dictionary where:
            * fans is a dictionary of dictionaries where the key is the location and the values:
                * status (boolean) - True if it's ok, false if it's broken
            * temperature is a dictionary of dictionaries where the key is the location and the values:
                * temperature (int) - Temperature in celsius the sensor is reporting.
                * is_alert (boolean) - True if the temperature is above the alert threshold
                * is_critical (boolean) - True if the temperature is above the critical threshold
            * power is a dictionary of dictionaries where the key is the PSU id and the values:
                * status (boolean) - True if it's ok, false if it's broken
                * capacity (int) - Capacity in W that the power supply can support
                * output (int) - Watts drawn by the system
            * cpu is a dictionary of dictionaries where the key is the ID and the values
                * %usage
            * available_ram (int) - Total amount of RAM installed in the device
            * used_ram (int) - RAM that is still free in the device
        """
        command = list()
        command.append('show environment cooling')
        command.append('show environment temperature')
        command.append('show environment power')
        output = self.device.run_commands(command)

        environment_counters = dict()
        environment_counters['fans'] = dict()
        environment_counters['temperature'] = dict()
        environment_counters['power'] = dict()
        environment_counters['cpu'] = dict()
        environment_counters['available_ram'] = ''
        environment_counters['used_ram'] = ''

        fans_output = output[0]
        temp_output = output[1]
        power_output = output[2]
        cpu_output = self.device.run_commands(['show processes top once'], encoding='text')[0]['output']

        ''' Get fans counters '''
        for slot in fans_output['fanTraySlots']:
            environment_counters['fans'][slot['label']] = dict()
            environment_counters['fans'][slot['label']]['status'] = slot['status'] == 'ok'

        ''' Get temp counters '''
        for slot in temp_output:
            try:
                for sensorsgroup in temp_output[slot]:
                    for sensor in sensorsgroup['tempSensors']:
                        environment_counters['temperature'][sensor['name']] = {
                            'temperature': sensor['currentTemperature'],
                            'is_alert': sensor['currentTemperature'] > sensor['overheatThreshold'],
                            'is_critical': sensor['currentTemperature'] > sensor['criticalThreshold']
                        }
            except:
                pass

        ''' Get power counters '''
        for _, item in power_output.iteritems():
            for id, ps in item.iteritems():
                environment_counters['power'][id] = {
                    'status': ps['state'] == 'ok',
                    'capacity': ps['capacity'],
                    'output': ps['outputPower']
                }

        ''' Get CPU counters '''
        m = re.search('(\d+.\d+)\%', cpu_output.splitlines()[2])
        environment_counters['cpu'][0] = float(m.group(1))
        m = re.search('(\d+)k\W+total\W+(\d+)k\W+used\W+(\d+)k\W+free', cpu_output.splitlines()[3])

        environment_counters['memory'] = {
            'available_ram': int(m.group(1)),
            'used_ram': int(m.group(2))
        }

        return environment_counters
