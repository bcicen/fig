from __future__ import unicode_literals
from __future__ import absolute_import
import logging

from .service import Service
from .container import Container
from docker.errors import APIError

log = logging.getLogger(__name__)


def sort_service_dicts(services):
    # Topological sort (Cormen/Tarjan algorithm).
    unmarked = services[:]
    temporary_marked = set()
    sorted_services = []

    get_service_names = lambda links: [link.split(':')[0] for link in links]

    def visit(n):
        if n['name'] in temporary_marked:
            if n['name'] in get_service_names(n.get('links', [])):
                raise DependencyError('A service can not link to itself: %s' % n['name'])
            if n['name'] in n.get('volumes_from', []):
                raise DependencyError('A service can not mount itself as volume: %s' % n['name'])
            else:
                raise DependencyError('Circular import between %s' % ' and '.join(temporary_marked))
        if n in unmarked:
            temporary_marked.add(n['name'])
            dependents = [m for m in services if (n['name'] in get_service_names(m.get('links', []))) or (n['name'] in m.get('volumes_from', []))]
            for m in dependents:
                visit(m)
            temporary_marked.remove(n['name'])
            unmarked.remove(n)
            sorted_services.insert(0, n)

    while unmarked:
        visit(unmarked[-1])

    return sorted_services


class Project(object):
    """
    A collection of services.
    """
    def __init__(self, name, services, client_maker):
        self.name = name
        self.clients = []
        self.client_maker = client_maker
        self.services = services
        for service in services:
            self.clients.append(service.client)

    @classmethod
    def from_dicts(cls, name, service_dicts, client_maker):
        """
        Construct a ServiceCollection from a list of dicts representing services.
        """
        project = cls(name, [], client_maker)
        for service_dict in sort_service_dicts(service_dicts):

            client = client_maker.get_client(service_dict)
            if client not in project.clients:
                project.clients.append(client)

            links = project.get_links(service_dict)
            volumes_from = project.get_volumes_from(service_dict)

            service = Service(client=client, project=name, links=links, volumes_from=volumes_from, **service_dict)
            project.services.append(service)

        return project

    @classmethod
    def from_config(cls, name, config, client_maker):
        dicts = []
        for service_name, service in list(config.items()):
            if not isinstance(service, dict):
                raise ConfigurationError('Service "%s" doesn\'t have any configuration options. All top level keys in your fig.yml must map to a dictionary of configuration options.' % service_name)
            service['name'] = service_name
            dicts.append(service)
        return cls.from_dicts(name, dicts, client_maker)

    def get_clients(self, remove_duplicated=True):

        if remove_duplicated:
            clients = []
            base_urls = set()
            for client in self.clients:
                if client.base_url not in base_urls:
                    base_urls.add(client.base_url)
                    clients.append(client)
            return clients

        return self.clients

    def get_service(self, name):
        """
        Retrieve a service by name. Raises NoSuchService
        if the named service does not exist.
        """
        for service in self.services:
            if service.name == name:
                return service

        raise NoSuchService(name)

    def get_services(self, service_names=None, include_links=False):
        """
        Returns a list of this project's services filtered
        by the provided list of names, or all services if service_names is None
        or [].

        If include_links is specified, returns a list including the links for
        service_names, in order of dependency.

        Preserves the original order of self.services where possible,
        reordering as needed to resolve links.

        Raises NoSuchService if any of the named services do not exist.
        """
        if service_names is None or len(service_names) == 0:
            return self.get_services(
                service_names=[s.name for s in self.services],
                include_links=include_links
            )
        else:
            unsorted = [self.get_service(name) for name in service_names]
            services = [s for s in self.services if s in unsorted]

            if include_links:
                services = reduce(self._inject_links, services, [])

            uniques = []
            [uniques.append(s) for s in services if s not in uniques]
            return uniques

    def get_links(self, service_dict):
        links = []
        if 'links' in service_dict:
            for link in service_dict.get('links', []):
                if ':' in link:
                    service_name, link_name = link.split(':', 1)
                else:
                    service_name, link_name = link, None
                try:
                    links.append((self.get_service(service_name), link_name))
                except NoSuchService:
                    raise ConfigurationError('Service "%s" has a link to service "%s" which does not exist.' % (service_dict['name'], service_name))
            del service_dict['links']
        return links

    def get_volumes_from(self, service_dict):
        volumes_from = []
        if 'volumes_from' in service_dict:
            for volume_name in service_dict.get('volumes_from', []):
                try:
                    service = self.get_service(volume_name)
                    volumes_from.append(service)
                except NoSuchService:
                    try:
                        for client in self.get_clients():
                            container = Container.from_id(client, volume_name)
                            volumes_from.append(container)
                    except APIError:
                        raise ConfigurationError('Service "%s" mounts volumes from "%s", which is not the name of a service or container.' % (service_dict['name'], volume_name))
            del service_dict['volumes_from']
        return volumes_from

    def start(self, service_names=None, **options):
        for service in self.get_services(service_names):
            service.start(**options)

    def stop(self, service_names=None, **options):
        for service in reversed(self.get_services(service_names)):
            service.stop(**options)

    def kill(self, service_names=None, **options):
        for service in reversed(self.get_services(service_names)):
            service.kill(**options)

    def restart(self, service_names=None, **options):
        for service in self.get_services(service_names):
            service.restart(**options)

    def build(self, service_names=None, no_cache=False):
        for service in self.get_services(service_names):
            if service.can_be_built():
                service.build(no_cache)
            else:
                log.info('%s uses an image, skipping' % service.name)

    def up(self,
           service_names=None,
           start_links=True,
           recreate=True,
           insecure_registry=False,
           do_build=True):
        running_containers = []
        for service in self.get_services(service_names, include_links=start_links):
            if recreate:
                for (_, container) in service.recreate_containers(
                        insecure_registry=insecure_registry,
                        do_build=do_build):
                    running_containers.append(container)
            else:
                for container in service.start_or_create_containers(
                        insecure_registry=insecure_registry,
                        do_build=do_build):
                    running_containers.append(container)

        return running_containers

    def pull(self, service_names=None, insecure_registry=False):
        for service in self.get_services(service_names, include_links=True):
            service.pull(insecure_registry=insecure_registry)

    def remove_stopped(self, service_names=None, **options):
        for service in self.get_services(service_names):
            service.remove_stopped(**options)

    def containers(self, service_names=None, stopped=False, one_off=False):
        return [Container.from_ps(client, container)
                for client in self.get_clients()
                for container in client.containers(all=stopped)
                for service in self.get_services(service_names)
                if service.has_container(container, one_off=one_off)]


    def _inject_links(self, acc, service):
        linked_names = service.get_linked_names()

        if len(linked_names) > 0:
            linked_services = self.get_services(
                service_names=linked_names,
                include_links=True
            )
        else:
            linked_services = []

        linked_services.append(service)
        return acc + linked_services


class NoSuchService(Exception):
    def __init__(self, name):
        self.name = name
        self.msg = "No such service: %s" % self.name

    def __str__(self):
        return self.msg


class ConfigurationError(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return self.msg


class DependencyError(ConfigurationError):
    pass
