#!/usr/bin/env python
# coding: utf-8


import sagemaker
import boto3
import sys
import os
import glob
import re
import subprocess
from time import gmtime, strftime
sys.path.append("common")
from misc import get_execution_role, wait_for_s3_object
from sagemaker.rl import RLEstimator, RLToolkit, RLFramework
from markdown_helper import *



# S3 bucket
boto_session = boto3.session.Session(
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "minio"), 
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "miniokey"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"))

s3Client = boto_session.resource("s3", use_ssl=False, endpoint_url=os.environ.get("S3_ENDPOINT_URL", "http://127.0.0.1:9000"))

sage_session = sagemaker.local.LocalSession(boto_session=boto_session, s3_client=s3Client)
s3_bucket = os.environ.get("MODEL_S3_BUCKET", "bucket") #sage_session.default_bucket() 
s3_output_path = 's3://{}/'.format(s3_bucket) # SDK appends the job name and output folder

# ### Define Variables

# We define variables such as the job prefix for the training jobs and s3_prefix for storing metadata required for synchronization between the training and simulation jobs


job_name_prefix = 'deepracer' # this should be MODEL_S3_PREFIX, but that already ends with "-sagemaker"

# create unique job name
tm = gmtime()
# job_name = s3_prefix = job_name_prefix + "-sagemaker"#-" + strftime("%y%m%d-%H%M%S", tm) #Ensure S3 prefix contains SageMaker
# s3_prefix_robomaker = job_name_prefix + "-robomaker"#-" + strftime("%y%m%d-%H%M%S", tm) #Ensure that the S3 prefix contains the keyword 'robomaker'
job_name = s3_prefix = "current"

# Duration of job in seconds (5 hours)
job_duration_in_seconds = 24 * 60 * 60

aws_region = sage_session.boto_region_name

if aws_region not in ["us-west-2", "us-east-1", "eu-west-1"]:
    raise Exception("This notebook uses RoboMaker which is available only in US East (N. Virginia), US West (Oregon) and EU (Ireland). Please switch to one of these regions.")
print("Model checkpoints and other metadata will be stored at: {}{}".format(s3_output_path, job_name))


s3_location = "s3://%s/%s" % (s3_bucket, s3_prefix)
print("Uploading to " + s3_location)

# Hyperparams
## Here we load hyperparameters from hyperparams.json file
with open('hyperparams.json', 'r', encoding='utf-8') as hp:
    hyper = eval(hp.read())
# Create dictionary that will be passed to estimator
hyperparameters = {"s3_bucket": s3_bucket,
        "s3_prefix": s3_prefix,
        "aws_region": aws_region,
        "model_metadata_s3_key": "s3://{}/custom_files/model_metadata.json".format(s3_bucket),
        "RLCOACH_PRESET": RLCOACH_PRESET,
        "batch_size": hyper["batch_size"],
        "beta_entropy": hyper["beta_entropy"],
        "discount_factor": hyper["discount_factor"],
        "e_greedy_value": hyper["e_greedy_value"],
        "epsilon_steps": hyper["epsilon_steps"],
        "exploration_type": hyper["exploration_type"],
        "loss_type": hyper["loss_type"],
        "lr": hyper["lr"],
        "num_episodes_between_training": hyper["num_episodes_between_training"],
        "num_epochs": hyper["num_epochs"],
        "stack_size": hyper["stack_size"],
        "term_cond_avg_score": hyper["term_cond_avg_score"],
        "term_cond_max_episodes": hyper["term_cond_max_episodes"]
        }
# Enable pretrained if setting existed
if hyper["pretrained"] > 0:
    hyperparameters.update({
        "pretrained_s3_bucket": "{}".format(s3_bucket),
        "pretrained_s3_prefix": "rl-deepracer-pretrained"
        })

metric_definitions = [
    # Training> Name=main_level/agent, Worker=0, Episode=19, Total reward=-102.88, Steps=19019, Training iteration=1
    {'Name': 'reward-training',
     'Regex': '^Training>.*Total reward=(.*?),'},
    
    # Policy training> Surrogate loss=-0.32664725184440613, KL divergence=7.255815035023261e-06, Entropy=2.83156156539917, training epoch=0, learning_rate=0.00025
    {'Name': 'ppo-surrogate-loss',
     'Regex': '^Policy training>.*Surrogate loss=(.*?),'},
     {'Name': 'ppo-entropy',
     'Regex': '^Policy training>.*Entropy=(.*?),'},
   
    # Testing> Name=main_level/agent, Worker=0, Episode=19, Total reward=1359.12, Steps=20015, Training iteration=2
    {'Name': 'reward-testing',
     'Regex': '^Testing>.*Total reward=(.*?),'},
]


# We use the RLEstimator for training RL jobs.
# 
# 1. Specify the source directory which has the environment file, preset and training code.
# 2. Specify the entry point as the training code
# 3. Specify the choice of RL toolkit and framework. This automatically resolves to the ECR path for the RL Container.
# 4. Define the training parameters such as the instance count, instance type, job name, s3_bucket and s3_prefix for storing model checkpoints and metadata. **Only 1 training instance is supported for now.**
# 4. Set the RLCOACH_PRESET as "deepracer" for this example.
# 5. Define the metrics definitions that you are interested in capturing in your logs. These can also be visualized in CloudWatch and SageMaker Notebooks.

# In[ ]:


RLCOACH_PRESET = "deepracer"

# 'local' for cpu, 'local_gpu' for nvidia gpu (and then you don't have to set default runtime to nvidia)
enable_gpu_training = os.environ.get('ENABLE_GPU_TRAINING', 'false')
if enable_gpu_training == 'false':
    instance_type = "local"
    image_name = "awsdeepracercommunity/deepracer-sagemaker:cpu"
else:
    instance_type = "local_gpu"
    image_name = "awsdeepracercommunity/deepracer-sagemaker:gpu"

estimator = RLEstimator(entry_point="training_worker.py",
                        source_dir='src',
                        dependencies=["common/sagemaker_rl"],
                        toolkit=RLToolkit.COACH,
                        toolkit_version='0.11',
                        framework=RLFramework.TENSORFLOW,
                        sagemaker_session=sage_session,
                        #bypass sagemaker SDK validation of the role
                        role="aaa/",
                        train_instance_type=instance_type,
                        train_instance_count=1,
                        output_path=s3_output_path,
                        base_job_name=job_name,
                        image_name=image_name,
                        train_max_run=job_duration_in_seconds, # Maximum runtime in seconds
                        hyperparameters=hyperparameters,
                        metric_definitions = metric_definitions,
						s3_client=s3Client
                        #subnets=default_subnets, # Required for VPC mode
                        #security_group_ids=default_security_groups, # Required for VPC mode
                    )

estimator.fit(job_name=job_name, wait=False)
