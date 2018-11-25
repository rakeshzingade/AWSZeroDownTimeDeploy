#!/usr/bin/python
#
# script execution: python zero-downtime-deploy.py -o <old-ami-id> -n <new-ami-id>
# optional parameter: -r <region>, default is us-east-1
# 
import getopt
import os, sys
import boto3
import time
import base64

program = os.path.basename(__file__)

def usage():
  print "Usage: "
  print "python ", program, "-o <old AMI-ID> -n <new AMI-ID> [-r <region>]"
  sys.exit()

#get az list from given region
def get_az(client, region):
  zones = []
  response = client.describe_availability_zones()
  for zone in response['AvailabilityZones']:
    if zone['State'] == 'available':
      zones.append(zone['ZoneName'])
  return zones

#get the running instance ids filtered on ami
#restrict to instance type t2.micro only
def get_ec2list(client, ami, region):
  instances = []
  zones = get_az(client, region)
  image_id = [ ami ]
  ec2filter = [ { 'Name': "availability-zone", 'Values': zones }, 
                { 'Name': "image-id", 'Values': image_id },
                { 'Name': "instance-state-name", 'Values': [ "running" ] }, 
                { 'Name': "instance-type", 'Values': [ "t1.micro" ] } ]
  response = client.describe_instances(Filters=ec2filter)
  if not response["Reservations"]:
    print "No instance is avaible with old AMI: ", ami
    sys.exit(0)
  print "All available instances with old ami:"
  for reservations in response["Reservations"]:
    for instance in reservations["Instances"]:
      AZ = instance['Placement']['AvailabilityZone']
      print "instance: ", instance["InstanceId"], "ami-id:", ami, "AZ: ", AZ
      instances.append(instance["InstanceId"])
  return instances

#func 
#output: list of elbs along with their associated ec2 instance ids
def get_elb(region):
  ec2elb_association = []
  ec2list = []
  client = boto3.client('elb', region)
  response = client.describe_load_balancers()
  for elbs in response['LoadBalancerDescriptions']:
    elbname = elbs['LoadBalancerName'] 
    for ec2dict in elbs['Instances']:
      ec2list.append(ec2dict['InstanceId'])
    tempdict = { 'elbname':elbname, 'Instances': ec2list }
    ec2elb_association.append(tempdict)
  return ec2elb_association    

#input: instance ids of oldamis and region
#return: list of 'elb name and associated old ec2 instance id'  
def search_elb_for_rollover_instances(instances, region):
  elb_ec2_list = []
  ec2elb_association = get_elb(region)
  for elem in ec2elb_association:
    elbname = elem['elbname']
    ec2ids = [ ec2 for ec2 in instances if ec2 in elem['Instances'] ]
    for ec2 in ec2ids:
      tempdict = { 'elbname': elbname, 'ec2id': ec2 }
      elb_ec2_list.append(tempdict)
  return elb_ec2_list

def is_instance_healthly(client, elbname, instanceid):
  ec2_instance = [ { 'InstanceId': instanceid } ]
  for i in range(30):
    response = client.describe_instance_health(LoadBalancerName=elbname, Instances=ec2_instance)
    if response['InstanceStates'][0]['State'] == 'InService':
      return True
    else:
      #wait
      time.sleep(10)
  return False

def is_ami_exists(client, ami_id):
  try:
    response = client.describe_images(ImageIds=[ ami_id ])
  except Exception, e:
    err = "Error: %s" % str(e)
    print(err)
    sys.exit(1)

  for images in response['Images']:
    if images['State'] == 'available':
      return True
  return False

def get_ec2_instance_details(client, instance_id):
  instance_details = {}
  instanceIds = [instance_id]
  response = client.describe_instances(InstanceIds=instanceIds)
  instance = response['Reservations'][0]['Instances'][0]
  attributes_resp = client.describe_instance_attribute(Attribute='userData', InstanceId=instance_id)
  userdata = attributes_resp['UserData']
  if userdata:
    userdata =  base64.b64decode(userdata['Value'])
  instance_details = {
    'InstanceId': instance_id,
    'Monitoring': instance['Monitoring'],
    'SubnetId': instance['SubnetId'],
    'VpcId': instance['VpcId'],
    'BlockDeviceMappings': instance['BlockDeviceMappings'],
    'SecurityGroups': instance['SecurityGroups'],
    'SourceDestCheck': instance['SourceDestCheck'],
    'Tags': instance['Tags'],
    'AZ': instance['Placement']['AvailabilityZone'],
    'KeyName': instance['KeyName'],
    'InstanceType': instance['InstanceType'],
    'UserData': userdata
  }
  return instance_details


def launch_ec2_instance(client, ami_id, launchdata):
  sgids = []
  for sg in launchdata['SecurityGroups']:
    sgids.append(sg['GroupId'])
  launched_ec2 = client.run_instances(ImageId=ami_id, \
             MinCount = 1, \
             MaxCount = 1, \
             SecurityGroupIds = sgids, \
             UserData = launchdata['UserData'], \
             SubnetId = launchdata['SubnetId'], \
             KeyName = launchdata['KeyName'], \
             InstanceType = launchdata['InstanceType'], \
             Placement = {'AvailabilityZone': launchdata['AZ'] })
  new_ec2_id = launched_ec2['Instances'][0]['InstanceId']
  AZ = launched_ec2['Instances'][0]['Placement']['AvailabilityZone']
  ImageId = launched_ec2['Instances'][0]['ImageId']
  print "new instance launched:", new_ec2_id, "in AZ: ", AZ, "ImageID: ", ImageId
  
  ec2 = boto3.resource('ec2')
  instance = ec2.Instance(new_ec2_id)
  instance_filter = [ { 'Name': 'instance-id', 'Values': [new_ec2_id] } ]
  instance.wait_until_running(instance_filter)
  time.sleep(60) #TODO bad hook use 'client.describe_instance_status()'
  return launched_ec2

def terminate_ec2_instance(client, ec2_instance_id):
  client.terminate_instances(InstanceIds=[ec2_instance_id])
  ec2 = boto3.resource('ec2')
  instance = ec2.Instance(ec2_instance_id)
  instance.wait_until_terminated()
  print "terminated ec2 instance:", ec2_instance_id

def register_instance_elb(client, elbname, instance_id):
  response = client.register_instances_with_load_balancer( \
             LoadBalancerName = elbname, \
             Instances = [ { 'InstanceId': instance_id } ])
  if response:
    print "registered ", instance_id, " to ", elbname

def rollback(elb, elbname, ec2, new_ec2_instance):
  #remove instance from elb
  print "rollback started..."
  response = elb.deregister_instances_from_load_balancer( \
                 LoadBalancerName = elbname, \
                 Instances = new_ec2_instance)
  if response['Instances'] == new_ec2_instance:
    print("removed new ec2 [%s] from elb", new_ec2_instance)
  else:
    print("failed to remove ec2 [%s] from elb", new_ec2_instance)
    print("please do the manual deletion, exiting..")
    sys.exit(1)
  
  response = ec2.terminate_instances(new_ec2_instance)
  print("terminated the new ec2 instance [%s]", new_ec2_instance['Instances'])
  print("rollback complete")

def main(argv):
  oldami = ''
  newami = ''
  region = 'us-east-1'
  if not argv:
    usage()
    sys.exit()
  try:
    opts, args = getopt.getopt(argv,"ho:n:r:", ["oldami=","newami=", "region="])
  except getopt.GetoptError:
    usage()
    sys.exit(2)
  for opt, arg in opts:
    if opt == '-h':
      usage()
      sys.exit()
    elif opt in ("-o", "--oldami"):
      oldami = arg
    elif opt in ("-n", "--newami"):
      newami = arg
    elif opt in ("-r", "--region"):
      region = arg
  
  if oldami == '' and newami == '':
    usage()
    sys.exit(2)

  #aws ec2 client
  try:
    ec2 = boto3.client('ec2', region_name = region)
  except Exception, e:
    err = "Error: %s" % str(e)
    print(err)
    sys.exit(1)

  #aws elb client
  try:
    elb = boto3.client('elb', region_name = region)
  except Exception, e:
    err = "Error: %s" % str(e)
    print(err)
    sys.exit(1)

  #check if both the ami exists
  if not is_ami_exists(ec2, oldami):
    print("given old ami [%s] is not available", oldami)
    sys.exit(1)
  if not is_ami_exists(ec2, newami):
    print("given new ami [%s] is not available", newami)
    sys.exit(1)
  
  #search for ec2 instance running with oldami 
  old_ec2_instances = get_ec2list(ec2, oldami, region)
   
  #get old ec2 instance configurations, attributes details
  ec2_instance_config = []
  for ec2instance in old_ec2_instances:
    ec2_instance_config.append(get_ec2_instance_details(ec2, ec2instance))

  #get the elb details of old ec2 instance
  elbname = ''
  elb_ec2_list = search_elb_for_rollover_instances(old_ec2_instances, region) 
  if not elb_ec2_list and elb_ec2_list[0]['elbname'] == '':
    print "elb not found, existing..."
    sys.exit(1)

  elbname = elb_ec2_list[0]['elbname']
  print "found elb: ", elbname
  
  #get ec2 behind elb
  ec2_behind_elb = []
  for ec2id_elb in elb_ec2_list:
    ec2_behind_elb.append(ec2id_elb['ec2id'])
    print "ec2 instance behind elb: ", ec2id_elb['ec2id']

  #launch new ec2 instance from existing config
  #TODO need to find better logic
  new_ec2_instances = []
  launchdata = ec2_instance_config[0]
  for i in range(len(ec2_behind_elb)):
    for j in range(len(ec2_instance_config)):
      if ec2_behind_elb[i] == ec2_instance_config[j]['InstanceId']:
        launchdata = ec2_instance_config[j]
        new_ec2_instances.append(launch_ec2_instance(ec2, newami, launchdata))

  #register the new instance to elb
  #get the map
  for instance in new_ec2_instances:
    instance_id = instance['Instances'][0]['InstanceId']
    register_instance_elb(elb, elbname, instance_id)
    #terminal the old ec2 instance if new ec2 instance shows healthy
    if is_instance_healthly(elb, elbname, instance_id):
      print "deploy successful, new instance-id = ", instance_id, " is healthy"
    else:#rollback
      print "deployment failed, starting rollback"
      for ec2_instance in new_ec2_instances:
        rollback(elb, elbname, ec2, new_ec2_instance)
      sys.exit(1)

  #remove old ec2 instances
  for ec2_instance in ec2_behind_elb:
    terminate_ec2_instance(ec2, ec2_instance)

if __name__ == "__main__":
  main(sys.argv[1:])

