# AWSZeroDownTimeDeploy
IaaC used for deploying/upgrading AWS infrastructure

## Script details

Suppose an web application is hosted on 3(or many) EC2 instances frontend by ELB. These instances are deployed in different AZs under one VPC. Considering this scenario we required zero downtime deploy script to upgrade the web application instance using new updated AMI-ID.

## Pre-requisites to execute the script
1. aws cli installed and configured
2. python boto3 library

## Execution steps
Execute the script as:
	*python zero-downtime-deploy.py -o \<old ami-id\> -n \<new ami-id\>*
	e.g.
	*python zero-downtime-deploy.py -o ami-0f9cf087c1f27d9b1 -n ami-0ac019f4fcb7cb7e6*
Optional Parameter: -r, --region (default is set to 'us-east-1')
And this will produce a flow chart:

## Script workflow
1. Read input parameters and validate input AMIs 
2. Get running ec2 instances ran by using existing AMI
3. Search elbs and ec2 behind the elb 
4. Add new ec2 instances with new AMI using old ec2 configuration parameters such as vpc, subnet, az, userdata etc
5. Register new ec2 instance with elb
6. Check the elb health of new ec2 instances 
7. Remove old ec2 instances once new ec2 instances becomes healthy 

## Testing effort
The code is tested for 3 ec2 instances deployed in different AZs behind 1 internet facing elb 
