# -*- coding: utf-8 -*-
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This module contains the main abstractions used by clusterdock topologies
to bring up clusters.
"""

import copy
import io
import logging
import os
import sys
import tarfile
import time
from collections import OrderedDict, namedtuple

import docker

from .config import defaults
from .exceptions import DuplicateClusterNameError, DuplicateHostnamesError
from .utils import (get_containers, generate_cluster_name, get_clusterdock_label,
                    in_docker_container, nested_get, wait_for_condition)

logger = logging.getLogger(__name__)

clusterdock_args = None
client = docker.from_env(timeout=300)

DEFAULT_NETWORK_TYPE = 'bridge'
LOCALTIME_MOUNT = True # Sync host time to Docker container by /etc/localtime
PRIVILEGED_CONTAINER = False # Give extended privileges to the container.


class Cluster:
    """The central abstraction for interacting with Docker container clusters.
    No Docker behavior is actually invoked until the start method is called.

    Args:
        *nodes: One or more :py:obj:`clusterdock.models.Node` instances.
    """

    def __init__(self, *nodes):
        self.nodes = nodes

        if clusterdock_args and clusterdock_args.cluster_name:
            clusters = {container.cluster_name for container in get_containers(clusterdock=True)}
            if clusterdock_args.cluster_name in clusters:
                raise DuplicateClusterNameError(name=clusterdock_args.cluster_name, clusters=clusters)
            else:
                self.name = clusterdock_args.cluster_name
        else:
            self.name = generate_cluster_name()

        if clusterdock_args and clusterdock_args.port:
            nodes_by_host = {node.hostname: node for node in self.nodes}
            for port in clusterdock_args.port:
                node = nodes_by_host.get(port.split(':')[0])
                port_value = port.split(':')[1]
                node.ports.append({port_value.split('->')[0]: port_value.split('->')[1]}
                                  if '->' in port_value else int(port_value))

        self.node_groups = {}
        for node in self.nodes:
            if node.group not in self.node_groups:
                logger.debug('Creating NodeGroup %s ...',
                             node.group)
                self.node_groups[node.group] = NodeGroup(node.group, node)
            else:
                self.node_groups[node.group].nodes.append(node)
            # Put this outside the if-else because, whether a new NodeGroup is created
            # or not, the node will be added to it.
            logger.debug('Adding node (%s) to NodeGroup %s ...',
                         node.hostname,
                         node.group)

    def start(self, network, pull_images=False, update_etc_hosts=True, pre_start_callback=None):
        """Start the cluster.

        Args:
            network (:obj:`str`): Name of the Docker network to use for the cluster.
            pull_images (:obj:`bool`, optional): Pull every Docker image needed by every
                :py:obj:`clusterdock.models.Node` instance, even if it exists locally.
                Default: ``False``
            update_etc_hosts (:obj:`bool`): Update the /etc/hosts file on the host with the hostname
                and IP address of the container. Default: ``True``
            pre_start_callback (:obj:`func`, optional): Function to be executed between the creation of the
                container and its start
        """
        logger.info('Starting cluster (%s) on network (%s) ...', self.name, network)
        self.network = network
        the_network = self._setup_network(name=self.network)

        if len(the_network.containers) != 0:
            containers_attached_to_network = [nested_get(container.attrs,
                                                         ['NetworkSettings',
                                                          'Networks',
                                                          self.network,
                                                          'Aliases',
                                                          0])
                                              for container in the_network.containers]
            logger.debug('Network (%s) currently has the followed containers attached: \n%s',
                         self.network,
                         '\n'.join('- {}'.format(container)
                                   for container in containers_attached_to_network))

            duplicate_hostnames = set(containers_attached_to_network) & set(node.hostname
                                                                            for node in self.nodes)
            if duplicate_hostnames:
                raise DuplicateHostnamesError(duplicates=duplicate_hostnames,
                                              network=self.network)

        for node in self:
            node.start(self.network, cluster_name=self.name, pull_images=pull_images,
                       pre_start_callback=pre_start_callback)

    def execute(self, command, **kwargs):
        """Execute a command on every :py:class:`clusterdock.models.Node` within the
            :py:class:`clusterdock.models.Cluster`.

        Args:
            command (:obj:`str`): Command to execute.
            **kwargs: Additional keyword arguments to pass to
                :py:meth:`clusterdock.models.Node.execute`.

        Returns:
            A :py:class:`collections.OrderedDict` of :obj:`str` instances (the FQDN of the node)
                mapping to the :py:class:`collections.namedtuple` instances returned by
                :py:meth:`clusterdock.models.Node.execute`.
        """
        return OrderedDict((node.fqdn, node.execute(command, **kwargs)) for node in self.nodes)

    def __iter__(self):
        for node in self.nodes:
            yield node

    def _setup_network(self, name):
        try:
            labels = {defaults.get('DEFAULT_DOCKER_LABEL_KEY'): get_clusterdock_label(self.name)}
            network = client.networks.create(name=name,
                                             driver=DEFAULT_NETWORK_TYPE,
                                             check_duplicate=True,
                                             labels=labels)
            logger.debug('Successfully created network (%s).', name)
        except docker.errors.APIError as api_error:
            if api_error.explanation == 'network with name {} already exists'.format(name):
                logger.warning('Network (%s) already exists. Continuing without creating ...',
                               name)
                network = client.networks.get(name)
            else:
                raise
        return network


class NodeGroup:
    """Abstraction representing a collection of Nodes that it could be useful to interact with
    enmasse. For example, a typical HDFS cluster could be seen as consisting of a 1 node group
    consisting of hosts with NameNodes and an n-1 node group of hosts with DataNodes.

    Args:
        name (:obj:`str`): The name by which to refer to the group.
        *nodes: One or more :py:class:`clusterdock.models.Node` instances.
    """
    def __init__(self, name, *nodes):
        self.name = name

        # We want the list of nodes to be mutable, so the tuple we get from *nodes
        # needs to be cast.
        self.nodes = list(nodes)

    def __iter__(self):
        for node in self.nodes:
            yield node

    def execute(self, command, **kwargs):
        """Execute a command on every :py:class:`clusterdock.models.Node` within the
            :py:class:`clusterdock.models.NodeGroup`.

        Args:
            command (:obj:`str`): Command to execute.
            **kwargs: Additional keyword arguments to pass to
                :py:meth:`clusterdock.models.Node.execute`.

        Returns:
            A :py:class:`collections.OrderedDict` of :obj:`str` instances (the FQDN of the node)
                mapping to the :py:class:`collections.namedtuple` instances returned by
                :py:meth:`clusterdock.models.Node.execute`.
        """
        return OrderedDict((node.fqdn, node.execute(command, **kwargs)) for node in self.nodes)


class Node:
    """Class representing a single cluster host.

    Args:
        hostname (:obj:`str`): Hostname of the node.
        group (:obj:`str`): :py:obj:`clusterdock.models.NodeGroup` to which the node should belong.
        image (:obj:`str`): Docker image with which to start the container.
        ports (:obj:`list`, optional): A list of container ports to expose to the host. Elements of
            the list could be integers (in which case a random port on the host will be chosen by
            the Docker daemon) or dictionaries (with the key being the host port and the value being
            the container port). Default: ``None``
        volumes (:obj:`list`, optional): A list of volumes to create for the node. Elements of the
            list could be dictionaries of bind volumes (i.e. key: the absolute path on the host,
            value: the absolute path in the container) or strings representing the names of
            Docker images from which to get volumes. As an example,
            ``[{'/var/www': '/var/www'}, 'my_super_secret_image']`` would create a bind mount of
            ``/var/www`` on the host and use any volumes from ``my_super_secret_image``.
            Default: ``None``
        devices (:obj:`list`, optional): Devices on the host to expose to the node. Default:
            ``None``
        **create_container_kwargs: Any other keyword arguments to pass directly to
            :py:meth:`docker.api.container.create_container`.
    """
    DEFAULT_CREATE_HOST_CONFIG_KWARGS = {
        # Add all capabilities to make containers host-like.
        'cap_add': ['ALL'],
        # Run without a seccomp profile.
        'security_opt': ['seccomp=unconfined']
    }

    DEFAULT_CREATE_CONTAINER_KWARGS = {
        # All nodes run in detached mode.
        'detach': True,
        'volumes': []
    }

    def __init__(self, hostname, group, image, ports=None, volumes=None, devices=None, environment=None,
                 **create_container_kwargs):
        self.hostname = hostname
        self.group = group
        self.image = image

        self.ports = ports or []
        self.volumes = volumes or []
        self.devices = devices or []
        self.environment = environment or {}
        self.create_container_kwargs = create_container_kwargs
        if clusterdock_args and clusterdock_args.clusterdock_config_directory:
            dir_path = clusterdock_args.clusterdock_config_directory
        else:
            dir_path = defaults.get('DEFAULT_CLUSTERDOCK_CONFIG_DIRECTORY')
        self.clusterdock_config_host_dir = os.path.realpath(os.path.expanduser(dir_path))
        logger.debug('self.clusterdock_config_host_dir = %s', self.clusterdock_config_host_dir)

        self.execute_shell = '/bin/sh'

    def start(self, network, cluster_name=None, pull_images=False, pre_start_callback=None):
        """Start the node.

        Args:
            network (:obj:`str`): Docker network to which to attach the container.
            cluster_name (:obj:`str`, optional): Cluster name to use for the Node. Default: ``None``
            pull_images (:obj:`bool`, optional): Pull every Docker image needed by this node instance,
                even if it exists locally.
                Default: ``False``
            pre_start_callback (:obj:`func`, optional): Function to be executed between the creation of the
                container and its start
        """
        self.fqdn = '{}.{}'.format(self.hostname, network)

        # Instantiate dictionaries for kwargs we'll pass when creating host configs
        # and the node's container itself.
        create_host_config_kwargs = copy.deepcopy(Node.DEFAULT_CREATE_HOST_CONFIG_KWARGS)
        create_container_kwargs = copy.deepcopy(dict(Node.DEFAULT_CREATE_CONTAINER_KWARGS,
                                                **self.create_container_kwargs))

        create_host_config_kwargs['privileged'] = PRIVILEGED_CONTAINER
        if LOCALTIME_MOUNT:
            # Mount in /etc/localtime to have container time match the host's.
            create_host_config_kwargs['binds'] = {os.path.join(self.clusterdock_config_host_dir, 'localtime'):
                                                  {'bind': '/etc/localtime', 'mode': 'rw'}}
            create_container_kwargs['volumes'].append('/etc/localtime')
        else:
            self.environment['TZ'] = os.readlink('/etc/localtime').split('zoneinfo/')[1]

        clusterdock_container_labels = {defaults.get('DEFAULT_DOCKER_LABEL_KEY'):
                                        get_clusterdock_label(cluster_name)}

        create_container_kwargs['labels'] = clusterdock_container_labels

        if self.volumes:
            # Instantiate empty lists to which we'll append elements as we traverse through
            # volumes. These populated lists will then get passed to either
            # :py:meth:`docker.api.client.APIClient.create_host_config` or
            # :py:meth:`docker.api.client.create_container`.
            binds = {}
            volumes = []

            volumes_from = []

            for volume in self.volumes:
                if isinstance(volume, list):
                    # List in the volumes list are Docker volumes to create.
                    volumes.extend(volume)
                elif isinstance(volume, dict):
                    # Dictionaries in the volumes list are bind volumes.
                    for host_directory, container_directory in volume.items():
                        logger.debug('Adding volume (%s) to container config ...',
                                     '{} => {}'.format(host_directory, container_directory))
                        binds[host_directory] = dict(bind=container_directory, mode='rw')
                        volumes.append(container_directory)
                elif isinstance(volume, str):
                    # Strings in the volume list are `volumes_from` images.
                    if pull_images:
                        logger.info('Node started with pull_images=True. '
                                    'Attempting to pull image (%s) ...', volume)
                        client.images.pull(volume)
                    else:
                        # Check for whether the image we need is present by trying to inspect it. If any
                        # NotFound exception is raised, make sure it's because the image is missing and then
                        # pull it before trying again.
                        try:
                            client.api.inspect_image(volume)
                        except docker.errors.NotFound as not_found:
                            if (not_found.response.status_code == 404 and
                                    'No such image' in not_found.explanation):
                                logger.info('Could not find %s locally. Attempting to pull ...', volume)
                                client.images.pull(volume)

                    container = client.containers.create(volume, labels=clusterdock_container_labels)
                    volumes_from.append(container.id)
                else:
                    element_type = type(volume).__name__
                    raise TypeError('Saw volume of type {} (must be dict or str).'.format(element_type))

            if volumes_from:
                create_host_config_kwargs['volumes_from'] = volumes_from

            if volumes:
                create_host_config_kwargs['binds'].update(binds)
                create_container_kwargs['volumes'] += volumes

        ports = []
        port_bindings = {}
        for port in self.ports:
            if isinstance(port, dict):
                for host_port, container_port in port.items():
                    logger.debug('Adding binding from host port %s to container port %s ...',
                                 host_port, container_port)
                    ports.append(container_port)
                    port_bindings[container_port] = host_port
            elif isinstance(port, int):
                ports.append(port)
                port_bindings[port] = None
            else:
                element_type = type(port).__name__
                raise TypeError('Saw port of type {} (must be dict or int).'.format(element_type))

        if self.environment:
            create_container_kwargs['environment']= self.environment

        if ports:
            create_container_kwargs['ports'] = ports
        if port_bindings:
            create_host_config_kwargs['port_bindings'] = port_bindings

        if self.devices:
            create_host_config_kwargs['devices'] = self.devices

        host_config = client.api.create_host_config(**create_host_config_kwargs)

        # Pass networking config to container at creation time to avoid issues with
        # DNS resolution.
        networking_config = client.api.create_networking_config({
            network: client.api.create_endpoint_config(aliases=[self.hostname])
        })

        logger.info('Starting node %s ...', self.fqdn)
        if pull_images:
            logger.info('Node started with pull_images=True. '
                        'Attempting to pull image (%s) ...', self.image)
            client.images.pull(self.image)
        else:
            # Check for whether the image we need is present by trying to inspect it. If any
            # NotFound exception is raised, make sure it's because the image is missing and then
            # pull it before trying again.
            try:
                client.api.inspect_image(self.image)
            except docker.errors.NotFound as not_found:
                if (not_found.response.status_code == 404 and
                        'No such image' in not_found.explanation):
                    logger.info('Could not find %s locally. Attempting to pull ...', self.image)
                    client.images.pull(self.image)

        # Since we need to use the low-level API to handle networking properly, we need to get
        # a container instance from the ID
        container_id = client.api.create_container(image=self.image,
                                                   hostname=self.fqdn,
                                                   host_config=host_config,
                                                   networking_config=networking_config,
                                                   **create_container_kwargs)['Id']

        if pre_start_callback:
            if not callable(pre_start_callback):
                raise TypeError('pre_start_callback() is not callable')
            logger.info('Running pre_start_callback() ...')
            pre_start_callback(container_id=container_id, node=self)

        client.api.start(container=container_id)

        # When the Container instance is created, the corresponding Docker container may not
        # be in a RUNNING state. Wait until it is (or until timeout takes place).
        self.container = client.containers.get(container_id=container_id)

        logger.debug('Connecting container (%s) to network (%s) ...',
                     self.container.short_id, network)

        # Wait for container to be in running state before moving on.
        def condition(container):
            container.reload()
            outcome = nested_get(container.attrs, ['State', 'Running'])
            logger.debug('Container running state evaluated to %s.', outcome)
            return outcome
        def success(time):
            logger.debug('Container reached running state after %s seconds.', time)
        def failure(timeout):
            logger.debug('Timed out after %s seconds waiting for container to reach running state.',
                         timeout)
        timeout_in_secs = 30
        wait_for_condition(condition=condition, condition_args=[self.container],
                           timeout=30, success=success, failure=failure)

        logger.debug('Reloading attributes for container (%s) ...', self.container.short_id)
        self.container.reload()

        self.ip_address = nested_get(self.container.attrs,
                                     ['NetworkSettings', 'Networks', network, 'IPAddress'])

        self.host_ports = {int(container_port.split('/')[0]): int(host_ports[0]['HostPort'])
                           for container_port, host_ports in nested_get(self.container.attrs,
                                                                        ['NetworkSettings',
                                                                         'Ports']).items()}
        if self.host_ports:
            logger.info('Created host port mapping (%s) for node (%s).',
                        '; '.join('{} => {}'.format(host_port, container_port)
                                  for host_port, container_port in self.host_ports.items()),
                        self.hostname)

        # If sshd is present, wait for the container's SSH daemon to come online before continuing.
        if self.execute('which sshd', quiet=True).exit_code == 0:
            def condition(node):
                sshd_status = node.execute('service sshd status', quiet=True).exit_code
                logger.debug('service sshd status returned %s.', sshd_status)
                return sshd_status == 0
            def success(time):
                logger.debug('SSH daemon came up after %s seconds.', time)
            def failure(timeout):
                logger.debug('Timed out after %s seconds waiting for SSH daemon to start.',
                             timeout)
            wait_for_condition(condition=condition, condition_args=[self],
                               timeout=30, success=success, failure=failure)

        # Add Docker container info to /etc/hosts on non-Mac instances to enable SOCKS5 proxy usage.
        if sys.platform != 'darwin' and not in_docker_container():
            self._add_node_to_etc_hosts()

    def stop(self, remove=True):
        """Stop the node and optionally removing the Docker container.

        Args:
            remove (:obj:`bool`, optional): Remove underlying Docker container. Default: ``True``
        """
        if not remove:
            self.container.stop()
        else:
            self.container.remove(v=True, force=True)

    def execute(self, command, user='root', quiet=False, detach=False):
        """Execute a command on the node.

        Args:
            command (:obj:`str`): Command to execute.
            user (:obj:`str`, optional): User with which to execute the command. Default: ``root``
            quiet (:obj:`bool`, optional): Run the command without showing any output. Default:
                ``False``
            detach (:obj:`bool`, optional): Run the command in detached mode. Default:
                ``False``

        Returns:
            A :py:class:`collections.namedtuple` instance with `exit_code` and `output` attributes.
        """
        logger.debug('Executing command (%s) on node (%s) ...', command, self.fqdn)
        exec_command = [self.execute_shell, '-c', command]
        logger.debug('Running docker exec with command (%s) ...', exec_command)
        exec_id = client.api.exec_create(self.container.id, exec_command, user=user)['Id']

        output = []
        stdout = []
        stderr = []
        for response_chunk in client.api.exec_start(exec_id, stream=True, demux=True, detach=detach):
            if not quiet:
                logger.debug('Got response link: %s', response_chunk)
            # Handle stdout
            if response_chunk[0]:
                stdout_ = response_chunk[0].decode()
                output.append(stdout_)
                stdout.append(stdout_)
            # Hande stderr
            if response_chunk[1]:
                stderr_ = response_chunk[1].decode()
                output.append(stderr_)
                stderr.append(stderr_)
        exit_code = client.api.exec_inspect(exec_id).get('ExitCode')
        return namedtuple('ExecuteSession', ['exit_code', 'output', 'stdout', 'stderr'])(exit_code=exit_code,
                                                                                         output=''.join(output),
                                                                                         stdout=''.join(stdout),
                                                                                         stderr=''.join(stderr))

    def get_file(self, path):
        """Get file from the node.

        Args:
            path (:obj:`str`): Absolute path to file.

        Returns:
            A :obj:`str` containing the contents of the file.
        """
        tarstream = io.BytesIO()
        for chunk in self.container.get_archive(path=path)[0]:
            tarstream.write(chunk)
        tarstream.seek(0)
        with tarfile.open(fileobj=tarstream) as tarfile_:
            for tarinfo in tarfile_.getmembers():
                return tarfile_.extractfile(tarinfo).read().decode()

    def put_file(self, path, contents):
        """Put file on the node.

        Args:
            path (:obj:`str`): Absolute path to file.
            contents: The contents of the file in :obj:`str` or :obj:`bytes` form.
        """
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w') as tarfile_:
            file_contents = contents.encode() if isinstance(contents, str) else contents
            tarinfo = tarfile.TarInfo(path)

            # We set the modification time to now because some systems (e.g. logging) rely upon
            # timestamps to determine whether to read config files.
            tarinfo.mtime = time.time()
            tarinfo.size = len(file_contents)
            tarfile_.addfile(tarinfo, io.BytesIO(file_contents))
        data.seek(0)

        self.container.put_archive(path='/', data=data)

    def commit(self, repository, tag=None, push=False, **kwargs):
        """Commit the Node's Docker container to a Docker image.

        Args:
            repository (:obj:`str`): The Docker repository to commit the image to.
            tag (:obj:`str`, optional): Docker image tag. Default: ``None``
            push (:obj:`bool`, optional): Push the image to Docker repository. Default: ``False``
            **kwargs: Additional keyword arguments to pass to
                :py:meth:`docker.models.Containers.Container.commit`
        """
        logger.debug('Committing `%s` with container id %s ...', self.fqdn, self.container.short_id)
        image = self.container.commit(repository=repository, tag=tag, **kwargs)
        logger.debug('%s repo tags committed with image id as %s', image.tags, image.short_id)
        if push:
            logger.debug('Pushing image of `%s` to repository %s ...', self.fqdn, repository)
            for line in client.api.push(repository, tag, stream=True, decode=True):
                line.pop('progressDetail', None) # take out too much detail
                logger.debug(line)
            logger.debug('%s repo tags pushed for `%s`, whose image id is %s',
                         image.tags, self.fqdn, image.short_id)

    def _add_node_to_etc_hosts(self):
        """Add node information to the Docker hosts' /etc/hosts file, exploiting Docker's
        permissions to do so without needing an explicit sudo.
        """
        image = 'alpine:latest'
        command = 'echo "{} {}  # clusterdock" >> /etc/hosts'.format(self.ip_address,
                                                                        self.fqdn)
        volumes = {'/etc/hosts': {'bind': '/etc/hosts', 'mode': 'rw'}}

        logger.debug('Adding %s to /etc/hosts ...', self.fqdn)
        client.containers.run(image=image,
                              command=[self.execute_shell, '-c', command],
                              volumes=volumes,
                              remove=True)
