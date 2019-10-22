c.KubeSpawner.http_timeout = 60 * 10 #Images are big, take time to pull, make it 10 mins for now because of storage issue
c.KubeSpawner.start_timeout = 60 * 10 #Images are big, take time to pull, make it 10 mins for now because of storage issue

import os

import json
import requests

c.JupyterHub.log_level = 'DEBUG'
# Do not shut down singleuser servers on restart
c.JupyterHub.cleanup_servers = False


import uuid
c.ConfigurableHTTPProxy.auth_token = str(uuid.uuid4())
public_service_dict = {
                        'PROXY_TOKEN': c.ConfigurableHTTPProxy.auth_token,
                        'PROXY_API_URL': 'http://%s:%d/' % ("127.0.0.1", 8082)
                    }
public_service_dict.update(os.environ)
c.JupyterHub.services = [
                            {
                                'name': 'public',
                                'command': ['bash', '-c', 'jupyter_publish_service'],
                                'environment': public_service_dict
                            }
                        ]
c.KubeSpawner.singleuser_extra_containers = [
        {
            "name": "nbviewer",
            "image": "nbviewer:latest",
            "ports": [
                {
                    "containerPort": 9090,
                    "protocol": "TCP"
                }
            ],
            "env" : [
                {
                    "name": "NBVIEWER_LOCALFILES",
                    "value": "/opt/app-root/src/public_notebooks"
                },
                {
                    "name": "NBVIEWER_TEMPLATES",
                    "value": "/opt/app-root/src"
                },
                {
                    "name": "NBVIEWER_PORT",
                    "value": "9090"
                },
                {
                    "name": "JUPYTERHUB_SERVICE_PREFIX",
                    "value": "/user/{username}/public/"
                },
                {
                    "name": "CACHE_EXPIRY_MIN",
                    "value": "30"
                },
                {
                    "name": "CACHE_EXPIRY_MAX",
                    "value": "60"
                },
                {
                    "name": "NO_CACHE",
                    "value": "true"
                }
            ],
        "volumeMounts": [
            {
                "mountPath": "/opt/app-root/src",
                "name": "data"
            }
        ]
        }
    ]


# Work out the public server address for the OpenShift REST API. Don't
# know how to get this via the REST API client so do a raw request to
# get it. Make sure request is done in a session so connection is closed
# and later calls against REST API don't attempt to reuse it. This is
# just to avoid potential for any problems with connection reuse.

server_url = "https://openshift.default.svc.cluster.local"
auth_info_url = '%s/.well-known/oauth-authorization-server' % server_url

with requests.Session() as session:
    response = session.get(auth_info_url, verify=False)
    data = json.loads(response.content.decode('UTF-8'))
    address = data["issuer"]

# Enable the OpenShift authenticator. The OPENSHIFT_URL environment
# variable must be set before importing the authenticator as it only
# reads it when module is first imported.

os.environ['OPENSHIFT_URL'] = address

from oauthenticator.openshift import OpenShiftOAuthenticator
c.JupyterHub.authenticator_class = OpenShiftOAuthenticator

# Override scope as oauthenticator code doesn't set it correctly.
# Need to lodge a PR against oauthenticator to have this fixed.

#OpenShiftOAuthenticator.scope = ['user:info']

# Setup authenticator configuration using details from environment.

service_name = os.environ['JUPYTERHUB_SERVICE_NAME']

service_account_name = '%s-hub' %  service_name
service_account_path = '/var/run/secrets/kubernetes.io/serviceaccount'

with open(os.path.join(service_account_path, 'namespace')) as fp:
    namespace = fp.read().strip()

client_id = 'system:serviceaccount:%s:%s' % (namespace, service_account_name)

c.OpenShiftOAuthenticator.client_id = client_id

with open(os.path.join(service_account_path, 'token')) as fp:
    client_secret = fp.read().strip()

c.OpenShiftOAuthenticator.client_secret = client_secret

# Work out hostname for the exposed route of the JupyterHub server. This
# is tricky as we need to use the REST API to query it.

from kubernetes import client, config
from openshift.dynamic import DynamicClient

config.load_incluster_config()

configuration = client.Configuration()
configuration.verify_ssl = False

oapi_client = DynamicClient(
    client.ApiClient(configuration=configuration)
)

routes = oapi_client.resources.get(kind='Route', api_version='route.openshift.io/v1')

route_list = routes.get(namespace=namespace)

host = None

for route in route_list.items:
    if route.metadata.name == service_name:
        host = route.spec.host

if not host:
    raise RuntimeError('Cannot calculate external host name for JupyterHub.')

c.OpenShiftOAuthenticator.oauth_callback_url = 'https://%s/hub/oauth_callback' % host

from jupyterhub_singleuser_profiles.profiles import SingleuserProfiles

from kubespawner import KubeSpawner
class OpenShiftSpawner(KubeSpawner):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.single_user_services = []
    self.single_user_profiles = SingleuserProfiles(server_url, client_secret, gpu_mode=os.environ.get('GPU_MODE'))
    self.gpu_mode = self.single_user_profiles.gpu_mode
    self.deployment_size = None

  def _options_form_default(self):
    imagestreams = oapi_client.resources.get(kind='ImageStream', api_version='image.openshift.io/v1')
    imagestream_list = imagestreams.get(namespace=namespace)

    cm_data = self.single_user_profiles.get_user_profile_cm(self.user.name)
    envs = cm_data.get('env', {})
    gpu = cm_data.get('gpu', 0)
    last_image = cm_data.get('last_selected_image', '')
    last_size = cm_data.get('last_selected_size', '')

    result = []
    for i in imagestream_list.items:
      if "-notebook" in i.metadata.name:
        name = i.metadata.name
        if not i.status.tags:
            continue
        for tag in i.status.tags:
          selected = ""
          image = "%s:%s" % (name, tag.tag)
          if image == last_image:
              selected = "selected=selected"
          result.append("<option value='%s' %s>%s</option>" % (image, selected, image))

    response = """
    <h3>JupyterHub Server Image</h3>
    <label for="custom_image">Select desired notebook image</label>
    <select class="form-control" name="custom_image" size="1">
    %s
    </select>
    \n
    """ % "\n".join(result)

    response += self.single_user_profiles.get_sizes_form(self.user.name)

    response += """
        <p>
            <label>GPU: </label>
            <input class="form-control" type="text" value="%s" name="gpu" />
        </p>
        """ % gpu

    aws_env = ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY']
    for key in aws_env:
        if key not in envs.keys():
            envs[key] = ""

    response += "<h3>Environment Variables</h3>"
    for key, val in envs.items():
        input_type = "text"
        if key in aws_env:
            input_type = "password"
        response += """
        <p>
        <label>%s: </label><input class="form-control" type="%s" value="%s" name="%s" />
        </p>
        """ % (key, input_type, val, key)

    response += """
    <p>
    <label>Variable name: </label><input class="form-control" type="text" name="variable_name_1" />
    <label>Variable value: </label><input class="form-control" type="text" name="variable_value_1" />
    </p>
    """

    return response

  def options_from_form(self, formdata):
    options = {}
    options['custom_image'] = formdata['custom_image'][0]
    options['size'] = formdata['size'][0]
    self.image_spec = options['custom_image']
    self.deployment_size = formdata['size'][0]
    del formdata['custom_image']
    del formdata['size']

    gpu = formdata['gpu'][0]
    del formdata['gpu']

    GPU_KEY = "nvidia.com/gpu"

    if int(gpu) > 0:
        if self.gpu_mode and self.gpu_mode == self.single_user_profiles.GPU_MODE_PRIVILEGED:
            self.privileged = True
        else:
            self.privileged = False

        if not self.extra_resource_guarantees:
            self.extra_resource_guarantees = {}
        self.extra_resource_guarantees[GPU_KEY] = gpu

        if not self.extra_resource_limits:
            self.extra_resource_limits = {}
        self.extra_resource_limits[GPU_KEY] = gpu
    else:
        if self.extra_resource_guarantees and self.extra_resource_guarantees.get(GPU_KEY):
            del self.extra_resource_guarantees[GPU_KEY]
        if self.extra_resource_limits and self.extra_resource_limits.get(GPU_KEY):
            del self.extra_resource_limits[GPU_KEY]
        self.privileged = False

    data = {} #'AWS_ACCESS_KEY_ID': formdata['AWS_ACCESS_KEY_ID'][0], 'AWS_SECRET_ACCESS_KEY': formdata['AWS_SECRET_ACCESS_KEY'][0]
    for key, val in formdata.items():
        if key.startswith("variable_name"):
            index = key.split("_")[-1]
            if len(formdata[key][0]) > 0:
                data[formdata[key][0]] = formdata['variable_value_%s' % index][0]
        elif not key.startswith("variable_value") and len(formdata[key][0]) > 0:
            data[key] = formdata[key][0]

    cm_data = {
        'env': data,
        'last_selected_image': self.singleuser_image_spec,
        'gpu': gpu,
        'last_selected_size': self.deployment_size
        }

    self.single_user_profiles.update_user_profile_cm(self.user.name, cm_data)
    return options



def apply_pod_profile(spawner, pod):
  spawner.single_user_profiles.load_profiles(username=spawner.user.name)
  profile = spawner.single_user_profiles.get_merged_profile(spawner.image_spec, user=spawner.user.name, size=spawner.deployment_size)
  return SingleuserProfiles.apply_pod_profile(spawner, pod, profile)

def setup_environment(spawner):
    spawner.single_user_profiles.load_profiles(username=spawner.user.name)
    spawner.single_user_profiles.setup_services(spawner, spawner.image_spec, spawner.user.name)

def clean_environment(spawner):
    spawner.single_user_profiles.clean_services(spawner, spawner.user.name)

c.JupyterHub.spawner_class = OpenShiftSpawner

c.OpenShiftSpawner.pre_spawn_hook = setup_environment
c.OpenShiftSpawner.post_stop_hook = clean_environment
c.OpenShiftSpawner.modify_pod_hook = apply_pod_profile
c.OpenShiftSpawner.cpu_limit = float(os.environ.get("SINGLEUSER_CPU_LIMIT", "1"))
c.OpenShiftSpawner.mem_limit = os.environ.get("SINGLEUSER_MEM_LIMIT", "1G")
c.OpenShiftSpawner.storage_pvc_ensure = True
c.KubeSpawner.storage_capacity = os.environ.get('SINGLEUSER_PVC_SIZE', '2Gi')
c.KubeSpawner.pvc_name_template = '%s-nb-{username}-pvc' % os.environ['JUPYTERHUB_SERVICE_NAME']
c.KubeSpawner.volumes = [dict(name='data', persistentVolumeClaim=dict(claimName=c.KubeSpawner.pvc_name_template))]
c.KubeSpawner.volume_mounts = [dict(name='data', mountPath='/opt/app-root/src')]
c.KubeSpawner.user_storage_class = os.environ.get("JUPYTERHUB_STORAGE_CLASS", c.KubeSpawner.user_storage_class)
admin_users = os.environ.get('JUPYTERHUB_ADMIN_USERS')
if admin_users:
    c.Authenticator.admin_users = set(admin_users.split(','))
