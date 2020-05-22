#!/usr/bin/env python

from base64 import b64encode
from datetime import datetime, timedelta
import ansible_runner
import copy
import hashlib
import json
import os
import requests
import shutil
import subprocess
import time
import yaml

import logging
logging.basicConfig(
    format = '%(asctime)s %(levelname)s %(message)s',
    level = os.environ.get('LOG_LEVEL', 'INFO')
)

class AnarchyRunner(object):

    def __init__(self):
        self.anarchy_url = os.environ.get('ANARCHY_URL', 'http://anarchy-operator:5000')
        self.domain = os.environ.get('OPERATOR_DOMAIN', 'anarchy.gpte.redhat.com')
        self.kubeconfig = os.environ.get('KUBECONFIG', None)
        self.pod_name = os.environ.get('POD_NAME', None)
        self.runner_name = os.environ.get('RUNNER_NAME', None)
        self.polling_interval = int(os.environ.get('POLLING_INTERVAL', 5))
        self.runner_dir = os.environ.get('RUNNER_DIR', '/anarchy-runner/ansible-runner')
        self.ansible_private_dir = os.environ.get('RUNNER_DIR', '/anarchy-runner/.ansible')
        self.runner_token = os.environ.get('RUNNER_TOKEN', None)

        if not self.kubeconfig:
            raise Exception('Environment variable KUBECONFIG must be set')
        if not self.pod_name:
            raise Exception('Environment variable POD_NAME must be set')
        if not self.runner_name:
            raise Exception('Environment variable RUNNER_NAME must be set')
        if not self.runner_token:
            raise Exception('Environment variable RUNNER_TOKEN must be set')

        if os.path.exists('/run/secrets/kubernetes.io/serviceaccount/namespace'):
            f = open('/run/secrets/kubernetes.io/serviceaccount/namespace')
            self.anarchy_namespace = f.read()
        elif 'ANARCHY_NAMESPACE' in os.environ:
            self.anarchy_namespace = os.environ['ANARCHY_NAMESPACE']
        else:
            self.anarchy_namespace = 'anarchy-operator'

        self.__init_runner_dir()

    def __init_runner_dir(self):
        if not os.path.exists(self.kubeconfig) \
        or 0 == os.path.getsize(self.kubeconfig):
            self.__write_kubeconfig()

    def __write_kubeconfig(self):
        kube_auth_token = open('/run/secrets/kubernetes.io/serviceaccount/token').read().strip()
        kube_ca_cert = open('/run/secrets/kubernetes.io/serviceaccount/ca.crt').read()
        kubeconfig_fh = open(self.kubeconfig, 'w')
        kubeconfig_fh.write(json.dumps({
            'apiVersion': 'v1',
            'clusters': [{
                'name': 'cluster',
                'cluster': {
                    'certificate-authority-data': \
                        b64encode(kube_ca_cert.encode('ascii')).decode('ascii'),
                    'server': 'https://kubernetes.default.svc.cluster.local'
                }
            }],
            'contexts': [{
                'name': 'ansible',
                'context': {
                    'cluster': 'cluster',
                    'namespace': self.anarchy_namespace,
                    'user': 'ansible'
                }
            }],
            'current-context': 'ansible',
            'users': [{
                'name': 'ansible',
                'user': {
                    'token': kube_auth_token
                }
            }]
        }))

    def clean_runner_dir(self):
        artifacts_dir = os.path.join(self.runner_dir, 'artifacts')
        for subdir in os.listdir(artifacts_dir):
            try:
                shutil.rmtree(os.path.join(artifacts_dir, subdir))
            except OSError as e:
                logging.warn('Failed to clean arifacts dir %s/%s: %s', artifacts_dir, subdir, str(e))

    def get_run(self):
        response = requests.get(
            self.anarchy_url + '/run',
            headers={'Authorization': 'Bearer {}:{}:{}'.format(self.runner_name, self.pod_name, self.runner_token)}
        )
        return response.json()

    def post_result(self, anarchy_run, result):
        run_name = anarchy_run['metadata']['name']
        subject_name = anarchy_run['spec']['subject']['name']
        requests.post(
            self.anarchy_url + '/run/' + run_name,
            headers={'Authorization': 'Bearer {}:{}:{}'.format(self.runner_name, self.pod_name, self.runner_token)},
            json=dict(result=result)
        )

    def run(self):
        anarchy_run = self.get_run()
        if not anarchy_run:
            logging.debug('No tasks to run')
            self.sleep()
            return

        # Extract governor and subject from AnarchyRun and set vars within
        # run spec into governor and subject for DWIM behavior.
        anarchy_governor = anarchy_run.pop('governor')
        anarchy_governor.update(anarchy_run['spec']['governor'])
        anarchy_subject = anarchy_run.pop('subject')
        anarchy_subject.update(anarchy_run['spec']['subject'])

        run_name = anarchy_run['metadata']['name']
        self.setup_ansible_galaxy_requirements(anarchy_run)
        self.clean_runner_dir()
        self.write_runner_vars(anarchy_run, anarchy_subject, anarchy_governor)
        self.write_runner_playbook(anarchy_run)
        logging.info('starting ansible runner')
        ansible_run = ansible_runner.interface.init_runner(
            playbook = 'main.yml',
            private_data_dir = self.runner_dir
        )
        ansible_run.config.env['ANSIBLE_STDOUT_CALLBACK'] = 'anarchy'
        ansible_run.run()
        self.post_result(anarchy_run, {
            'rc': ansible_run.rc,
            'status': ansible_run.status,
            'ansibleRun': yaml.safe_load(
                open(self.ansible_private_dir + '/anarchy-result.yaml').read()
            )
        })

    def setup_ansible_galaxy_requirements(self, anarchy_run):
        requirements = anarchy_run['spec'].get('ansibleGalaxyRequirements', None)
        requirements_md5 = hashlib.md5(json.dumps(
            requirements,sort_keys=True, separators=(',', ':')
        ).encode('utf-8')).hexdigest()
        requirements_dir = self.ansible_private_dir + '/requirements-' + requirements_md5
        requirements_file = requirements_dir + '/requirements.yaml'
        ansible_collections_dir = self.ansible_private_dir + '/collections'
        ansible_roles_dir = self.ansible_private_dir + '/roles'
        if not os.path.exists(requirements_dir):
            os.mkdir(requirements_dir)
            os.mkdir(requirements_dir + '/collections')
            os.mkdir(requirements_dir + '/roles')
            with open(requirements_file, 'w') as fh:
                yaml.safe_dump(requirements, stream=fh)
        if os.path.lexists(ansible_collections_dir):
            os.unlink(ansible_collections_dir)
        if os.path.lexists(ansible_roles_dir):
            os.unlink(ansible_roles_dir)
        os.symlink(requirements_dir + '/collections', ansible_collections_dir)
        os.symlink(requirements_dir + '/roles', ansible_roles_dir)
        if requirements and 'collections' in requirements:
            subprocess.run(['ansible-galaxy', 'collection', 'install', '-r', requirements_file])
        if requirements:
            subprocess.run(['ansible-galaxy', 'role', 'install', '-r', requirements_file])

    def sleep(self):
        time.sleep(self.polling_interval)

    def write_runner_playbook(self, anarchy_run):
        playbook_file = self.runner_dir + '/project/main.yml'
        run_spec = anarchy_run['spec']
        plays = [dict(
            name = 'Anarchy Ansible Run',
            hosts = 'localhost',
            connection = 'local',
            gather_facts = False,
            pre_tasks = run_spec.get('preTasks', []),
            roles = run_spec.get('roles', []),
            tasks = run_spec.get('tasks', []),
            post_tasks = run_spec.get('postTasks', [])
        )]
        open(playbook_file, mode='w').write(json.dumps(plays))

    def write_runner_vars(self, anarchy_run, anarchy_subject, anarchy_governor):
        extravars = copy.deepcopy(anarchy_run['spec'].get('vars', {}))
        extravars.update({
            'anarchy_governor': anarchy_governor,
            'anarchy_governor_name': anarchy_run['spec']['governor']['name'],
            'anarchy_namespace': self.anarchy_namespace,
            'anarchy_operator_domain': self.domain,
            'anarchy_run': anarchy_run,
            'anarchy_run_name': anarchy_run['metadata']['name'],
            'anarchy_run_pod_name': self.pod_name,
            'anarchy_run_timestamp': datetime.utcnow().strftime('%FT%TZ'),
            'anarchy_runner_name': self.runner_name,
            'anarchy_runner_token': self.runner_token,
            'anarchy_subject': anarchy_subject,
            'anarchy_subject_name': anarchy_run['spec']['subject']['name'],
            'anarchy_url': self.anarchy_url,
        })
        anarchy_action = anarchy_run['spec'].get('action', None)
        if anarchy_action:
            extravars.update({
                'anarchy_action': anarchy_action,
                'anarchy_action_name': anarchy_action['name']
            })
        anarchy_action_config = anarchy_run['spec'].get('actionConfig', None)
        if anarchy_action_config:
            extravars.update({
                'anarchy_action_config': anarchy_action_config,
                'anarchy_action_config_name': anarchy_action_config['name']
            })
        open(self.runner_dir + '/env/extravars', mode='w').write(json.dumps(extravars))


anarchy_runner = AnarchyRunner()

def main():
    while True:
        try:
            anarchy_runner.run()
        except Exception:
            logging.exception('Error in runner loop')
            anarchy_runner.sleep()

if __name__ == '__main__':
    main()
