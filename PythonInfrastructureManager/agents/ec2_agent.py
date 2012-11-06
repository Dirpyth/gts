from datetime import datetime, timedelta
from os import environ
from os.path import abspath
from os.path import exists
import re
import sys
from time import sleep
from agents.base_agent import BaseAgent
from utils import utils

__author__ = 'hiranya'

# A regular expression that matches fully qualified domain names, used to
# parse output from describe-instances to see the FQDNs for machines
# currently running.
FQDN_REGEX = '[\w\d\.\-]+'

# The maximum amount of time, in seconds, that we are willing to wait for
# a virtual machine to start up, from the initial run-instances request.
# Setting this value is a bit of an art, but we choose the value below
# because our image is roughly 10GB in size, and if Eucalyptus doesn't
# have the image cached, it could take half an hour to get our image
# started.
MAX_VM_CREATION_TIME = 1800

# The amount of time that run_instances waits between each describe-instances
# request. Setting this value too low can cause Eucalyptus to interpret
# requests as replay attacks.
SLEEP_TIME = 20

PARAM_CREDENTIALS       = 'credentials'
PARAM_GROUP             = 'group'
PARAM_IMAGE_ID          = 'image_id'
PARAM_INSTANCE_TYPE     = 'instance_type'
PARAM_KEYNAME           = 'keyname'

REQUIRED_EC2_RUN_INSTANCES_PARAMS = (
    PARAM_CREDENTIALS,
    PARAM_GROUP,
    PARAM_IMAGE_ID,
    PARAM_INSTANCE_TYPE,
    PARAM_KEYNAME
)

class EC2Agent(BaseAgent):

    def __init__(self):
        self.prefix = 'ec2'

    def set_environment_variables(self, parameters, cloud_num):
        variables = parameters[PARAM_CREDENTIALS]
        prefix = 'CLOUD' + str(cloud_num) + '_'
        for key, value in variables.items():
            if key.startswith(prefix):
                environ[key[len(prefix):]] = value

        if environ.has_key('EC2_JVM_ARGS'):
            del(environ['EC2_JVM_ARGS'])
        ec2_keys_dir = abspath('/etc/appscale/keys/cloud' + str(cloud_num))
        environ['EC2_PRIVATE_KEY'] = ec2_keys_dir + '/mykey.pem'
        environ['EC2_CERT'] = ec2_keys_dir + '/mycert.pem'
        print 'Setting private key to: {0} and certificate to: {1}'.format(
            environ['EC2_PRIVATE_KEY'], environ['EC2_CERT'])

    def configure_instance_security(self, parameters):
        keyname = parameters[PARAM_KEYNAME]
        cloud = parameters['cloud']
        ssh_key = abspath('/etc/appscale/keys/{0}/{1}.key'.format(cloud, keyname))
        print 'About to spawn EC2 instances - Expecting to find a key at', ssh_key
        #TODO: log obscured
        if not exists(ssh_key):
            print 'Creating keys/security group for', cloud
            #TODO: generate key
            #TODO: create appscale security group
            return True
        else:
            print 'Not creating keys/security group for', cloud
            return False

    def has_required_parameters(self, parameters):
        for param in REQUIRED_EC2_RUN_INSTANCES_PARAMS:
            if not utils.has_parameter(param, parameters):
                return False, 'no ' + param
        return True, 'none'

    def describe_instances(self, parameters):
        keyname = parameters[PARAM_KEYNAME]
        describe_instances = utils.shell(self.prefix + '-describe-instances 2>&1')
        print 'describe-instances says', describe_instances
        fqdn_regex = re.compile('\s+({0})\s+({0})\s+running\s+{1}\s'.format(FQDN_REGEX, keyname))
        instance_regex = re.compile('INSTANCE\s+(i-\w+)')
        all_ip_addresses = utils.flatten(fqdn_regex.findall(describe_instances))
        instances = utils.flatten(instance_regex.findall(describe_instances))
        public_ips, private_ips = self.get_ip_addresses(all_ip_addresses)
        return public_ips, private_ips, instances

    def run_instances(self, count, parameters, security_configured):
        image_id = parameters[PARAM_IMAGE_ID]
        instance_type = parameters[PARAM_INSTANCE_TYPE]
        keyname = parameters[PARAM_KEYNAME]
        group = parameters[PARAM_GROUP]
        cloud = parameters['cloud']
        spot = False

        print '[{0}] [{1}] [{2}] [{3}] [ec2] [{4}] [{5}] [{6}]'.format(count,
            image_id, instance_type, keyname, cloud, group, spot)

        start_time = datetime.now()
        active_public_ips = []
        active_private_ips = []
        active_instances = []
        print 'EC2_URL = [{0}]'.format(environ['EC2_URL'])
        while True:
            active_public_ips, active_private_ips, active_instances = \
                self.describe_instances(parameters)
            # If security has been configured on this agent just now,
            # that's an indication that this is a fresh cloud deployment.
            # As such it's not expected to have any running VMs.
            if len(active_instances) > 0 or security_configured:
                break

        args = '-k {0} -n {1} --instance-type {2} --group {3} {4}'.format(keyname,
            count, instance_type, group, image_id)
        if spot:
            price = self.get_optimal_spot_price(instance_type)
            command_to_run = '{0}-request-spot-instances -p {1} {2}'.format(self.prefix, price, args)
        else:
            command_to_run = '{0}-run-instances {1}'.format(self.prefix, args)

        while True:
            print command_to_run
            run_instances = utils.shell(command_to_run)
            print 'Run instances says', run_instances
            status, command_to_run = self.run_instances_response(command_to_run, run_instances)
            if status:
                break
            print 'sleepy time'
            sleep(5)

        instances = []
        public_ips = []
        private_ips = []
        sleep(10)

        end_time = datetime.now() + timedelta(0, MAX_VM_CREATION_TIME)
        now = datetime.now()
        while now < end_time:
            describe_instances = utils.shell(self.prefix + '-describe-instances 2>&1')
            print '[{0}] {1} seconds left...'.format(now, (end_time - now).seconds)
            print describe_instances
            fqdn_regex = re.compile('\s+({0})\s+({0})\s+running\s+{1}\s'.format(FQDN_REGEX, keyname))
            instance_regex = re.compile('INSTANCE\s+(i-\w+)')
            all_ip_addresses = utils.flatten(fqdn_regex.findall(describe_instances))
            instances = utils.flatten(instance_regex.findall(describe_instances))
            public_ips, private_ips = self.get_ip_addresses(all_ip_addresses)
            public_ips = utils.diff(public_ips, active_public_ips)
            private_ips = utils.diff(private_ips, active_private_ips)
            instances = utils.diff(instances, active_instances)
            if count == len(public_ips):
                break
            sleep(SLEEP_TIME)
            now = datetime.now()

        if not public_ips:
            sys.exit('No public IPs were able to be procured within the time limit')

        if len(public_ips) != count:
            for index in range(0, len(public_ips)):
                if public_ips[index] == '0.0.0.0':
                    instance_to_term = instances[index]
                    print 'Instance {0} failed to get a public IP address and is being terminated'.\
                        format(instance_to_term)
                    utils.shell(self.prefix + '-terminate-instances ' + instance_to_term)
            pass

        end_time = datetime.now()
        total_time = end_time - start_time
        if spot:
            print 'TIMING: It took {0} seconds to spawn {1} spot instances'.format(
                total_time.seconds, count)
        else:
            print 'TIMING: It took {0} seconds to spawn {1} regular instances'.format(
                total_time.seconds, count)
        return instances, public_ips, private_ips

    def run_instances_response(self, command, output):
        if output.find('Please try again later') != -1:
            print 'Error with run instances: {0}. Will try again in a moment.'.format(output)
            return False, command
        elif output.find('try --addressing private') != -1:
            print 'Need to retry with addressing private. Will try again in a moment.'
            return False, command + ' --addressing private'
        elif output.find('PROBLEM') != -1:
            print 'Error:', output
            sys.exit('Saw the following error from EC2 tools: {0}'.format(output))
        else:
            print 'Run instances message sent successfully. Waiting for the image to start up.'
            return True, command

    def get_optimal_spot_price(self, instance_type):
        return None

    def get_ip_addresses(self, all_addresses):
        if len(all_addresses) % 2 != 0:
            sys.exit('IP address list is not of even length')
        reported_public = []
        reported_private = []
        for index in range(0, len(all_addresses)):
            if index % 2 == 0:
                reported_public.append(all_addresses[index])
            else:
                reported_private.append(all_addresses[index])
        print 'Reported public IPs:', reported_public
        print 'Reported private IPs:', reported_private

        actual_public = []
        actual_private = []
        for index in range(0, len(reported_public)):
            public = reported_public[index]
            private = reported_private[index]
            if public != '0.0.0.0' and private != '0.0.0.0':
                actual_public.append(public)
                actual_private.append(private)

        for index in range(0, len(actual_private)):
            #TODO: Convert FQDN to IP
            pass
        return actual_public, actual_private















