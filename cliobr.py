import time
import os
import re
import subprocess
import click
import logging
import ovirtsdk4 as sdk
import ovirtsdk4.types as types
from pathlib import Path
import helpers


FORMAT = '%(asctime)s %(levelname)s %(message)s'
AgentVM = 'backuprestore'
Description = 'cli-ovirt-backup'
VERSION = '0.4.3'


def print_version(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    click.echo(VERSION)
    ctx.exit()


@click.group()
@click.option('--version', is_flag=True, callback=print_version, expose_value=False, is_eager=True)
def cli():
    pass


@cli.command()
@click.argument('vmname')
@click.option(
    '--username', '-u', envvar='OVIRTUSER', default='admin@internal', show_default=True, help='username for oVirt API'
)
@click.option(
    '--password', '-p', envvar='OVIRTPASS', required=True, help='password for oVirt user'
)
@click.option(
    '--ca', '-c', envvar='OVIRTCA', required=True, type=click.Path(), help='path for ca certificate of Manager'
)
@click.option(
    '--url', '-U', envvar='OVIRTURL', required=True, help='url for oVirt API https://manager.example.com/ovirt-engine/api'
)
@click.option(
    '--backup-path', '-b', envvar='BACKUPPATH', type=click.Path(), default='/ovirt-backup', show_default=True, help='path of backups'
)
@click.option(
    '--log', '-l', envvar='OVIRTLOG', type=click.Path(), default='/var/log/cli-ovirt-backup.log', show_default=True, help='path log file'
)
@click.option('--debug', '-d', is_flag=True, default=False, help='debug mode')
@click.option('--archive', '-a', is_flag=True, default=True, help='archive backup')
def backup(username, password, ca, vmname, url, debug, backup_path, log, archive):
    logging.basicConfig(level=logging.DEBUG, format=FORMAT,
                        filename=log)
    connection = sdk.Connection(
        url=url,
        username=username,
        password=password,
        ca_file=ca,
        debug=debug,
        log=logging.getLogger(),
    )

    logging.info('Connected to the server.')
    if debug:
        click.echo('Connected to the server.')

    # Get the reference to the root of the services tree:
    system_service = connection.system_service()

    # Get the reference to the service that we will use to send events to
    # the audit log:
    events_service = system_service.events_service()

    # In order to send events we need to also send unique integer ids. These
    # should usually come from an external database, but in this example we
    # will just generate them from the current time in seconds since Jan 1st
    # 1970.
    event_id = int(time.time())

    # Get the reference to the service that manages the virtual machines:
    vms_service = system_service.vms_service()

    vm = helpers.vmobj(vms_service, vmname)
    ts = str(event_id)

    message = (
        'Backup of virtual machine \'{}\' using snapshot \'{}\' is '
        'starting.'.format(vm.name, Description)
    )
    helpers.send_events(events_service, event_id,
                        types, vm, Description, message)

    TIMESTAMP = ts.replace('.', '')
    FILENAME = vm.name+'-'+TIMESTAMP
    DIR_SAVE = backup_path+'/'+FILENAME

    logging.info(
        'Found data virtual machine \'{}\', the id is \'{}\'.'.format(
            vm.name, vm.id)
    )
    if debug:
        click.echo(
            'Found data virtual machine \'{}\', the id is \'{}\'.'.format(vm.name, vm.id))
    vmAgent = helpers.vmobj(vms_service, AgentVM)
    logging.info(
        'Found data virtual machine \'{}\', the id is \'{}\'.'.format(
            vmAgent.name, vmAgent.id)
    )
    if debug:
        click.echo(
            'Found data virtual machine \'{}\', the id is \'{}\'.'.format(vm.name, vm.id))

    helpers.createdir(DIR_SAVE)
    logging.info('Creating directory {}.'.format(DIR_SAVE + '/'))
    if debug:
        click.echo('Creating directory {}.'.format(DIR_SAVE + '/'))
    # Find the services that manage the data and agent virtual machines:
    data_vm_service = vms_service.vm_service(vm.id)
    agent_vm_service = vms_service.vm_service(vmAgent.id)

    ovf_file = helpers.writeconfig(vm, DIR_SAVE + '/')
    logging.info('Wrote OVF to file \'{}\''.format(
        os.path.abspath(ovf_file)))
    if debug:
        click.echo('Wrote OVF to file \'{}\''.format(
            os.path.abspath(ovf_file)))

    snaps_service = data_vm_service.snapshots_service()

    snap = helpers.createsnapshot(snaps_service, types, Description)
    logging.info('Sent request to create snapshot \'{}\', the id is \'{}\'.'.format(
        snap.description, snap.id))
    if debug:
        click.echo('Sent request to create snapshot \'{}\', the id is \'{}\'.'.format(
            snap.description, snap.id))

    snap_service = snaps_service.snapshot_service(snap.id)
    helpers.waitingsnapshot(snap, types, logging, time,
                            snap_service, click, debug)

    # Retrieve the descriptions of the disks of the snapshot:
    snap_disks_service = snap_service.disks_service()
    snap_disks = snap_disks_service.list()

    # Attach disk service
    attachments_service = agent_vm_service.disk_attachments_service()

    attachments = helpers.populateattachments(
        snap_disks, snap, attachments_service, types, logging, click, debug)

    for attach in attachments:
        logging.info(
            'Attached disk \'{}\' to the agent virtual machine.'.format(
                attach.disk.id)
        )
        if debug:
            click.echo(
                'Attached disk \'{}\' to the agent virtual machine.'.format(
                    attach.disk.id)
            )

    block_devices = helpers.getdevices()
    devices = {}
    for i in range(len(attachments)):
        devices[attachments[i].disk.id] = '/dev/' + block_devices[i]

    helpers.converttoqcow2(devices, DIR_SAVE + '/', debug, logging, click)

    for attach in attachments:
        attachment_service = attachments_service.attachment_service(attach.id)
        attachment_service.remove()
        logging.info(
            'Detached disk \'{}\' to from the agent virtual machine.'.format(
                attach.disk.id)
        )
        if debug:
            click.echo(
                'Detached disk \'{}\' to from the agent virtual machine.'.format(
                    attach.disk.id)
            )
    # Remove the snapshot:
    snap_service.remove()
    logging.info('Removed the snapshot \'{}\'.'.format(snap.description))
    if debug:
        click.echo('Removed the snapshot \'{}\'.'.format(snap.description))

    if archive:
        import shutil
        logging.info('Archiving \'{}\' in \'{}.tar.gz\''.format(
            FILENAME, FILENAME))
        shutil.make_archive(FILENAME, 'gztar', backup_path)
        shutil.rmtree(DIR_SAVE)
        if debug:
            click.echo('Archiving \'{}\' in \'{}.tar.gz\''.format(
                FILENAME, FILENAME))
    event_id += 1
    message = (
        'Backup of virtual machine \'{}\' using snapshot \'{}\' is '
        'completed.'.format(vm.name, Description)
    )
    helpers.send_events(events_service, event_id,
                        types, vm, Description, message)

    # Finish the connection to the VM Manager
    connection.close()
    logging.info('Disconnected to the server.')
    if debug:
        click.echo('Disconnected to the server.')


@cli.command()
@click.argument('filename')
@click.option(
    '--username', '-u', envvar='OVIRTUSER', default='admin@internal', show_default=True, help='username for oVirt API'
)
@click.option(
    '--password', '-p', envvar='OVIRTPASS', required=True, help='password for oVirt user'
)
@click.option(
    '--ca', '-c', envvar='OVIRTCA', required=True, type=click.Path(), help='path for ca certificate of Manager'
)
@click.option(
    '--url', '-U', envvar='OVIRTURL', required=True, help='url for oVirt API https://manager.example.com/ovirt-engine/api'
)
@click.option(
    '--storage-domain', '-s', envvar='OVIRTSD', required=True, help='Name of oVirt/RHV Storage Domain'
)
@click.option(
    '--cluster', '-C', envvar='OVIRTCLUSTER', required=True, help='Name of oVirt/RHV Cluster'
)
@click.option(
    '--restore-path', '-r', envvar='BACKUPPATH', type=click.Path(), default='/ovirt-backup', show_default=True, help='path of backups'
)
@click.option(
    '--log', '-l', envvar='OVIRTLOG', type=click.Path(), default='/var/log/cli-ovirt-backup.log', show_default=True, help='path log file'
)
@click.option('--debug', '-d', is_flag=True, default=False, help='debug mode')
def restore(username, password, filename, ca, url, storage_domain, log, debug, restore_path, cluster):

    logging.basicConfig(level=logging.DEBUG, format=FORMAT,
                        filename=log)
    connection = sdk.Connection(
        url=url,
        username=username,
        password=password,
        ca_file=ca,
        debug=debug,
        log=logging.getLogger(),
    )

    logging.info('Connected to the server.')
    if debug:
        click.echo('Connected to the server.')

    # Get the reference to the root of the services tree:
    system_service = connection.system_service()

    # Get the reference to the service that we will use to send events to
    # the audit log:
    events_service = system_service.events_service()

    disks_service = system_service.disks_service()

    # In order to send events we need to also send unique integer ids. These
    # should usually come from an external database, but in this example we
    # will just generate them from the current time in seconds since Jan 1st
    # 1970.
    event_id = int(time.time())

    name = re.sub("\-.*$", '', filename)

    message = (
        'Restore of virtual machine \'{}\' using file \'{}\' is '
        'starting.'.format(name, filename)
    )

    logging.info(message)
    if debug:
        click.echo(message)

    helpers.send_events(events_service, event_id,
                        types, Description, message)

    # Get the reference to the service that manages the virtual machines:
    vms_service = system_service.vms_service()
    disks_metadata, extracted_path = helpers.getinfoqcow2(
        filename, restore_path, click)

    xml_file = Path(extracted_path).glob('**/*.ovf')

    ovf, ovf_str = helpers.ovf_parse(xml_file)

    disks = []
    namespace = '{http://schemas.dmtf.org/ovf/envelope/1/}'

    for qemu_disk, ovf_disk in list(zip(disks_metadata, ovf.iter("Disk"))):
        if ovf_disk.get(namespace+'volume-format') == 'COW':
            disk_format = types.DiskFormat.COW
        else:
            disk_format = types.DiskFormat.RAW
        if ovf_disk.get(namespace+'boot'):
            boot = True
        new_disk = disks_service.add(
            disk=types.Disk(
                id=ovf_disk.get(namespace + 'diskId'),
                name=ovf_disk.get(namespace + 'disk-alias'),
                description=ovf_disk.get(namespace + 'disk-description'),
                format=disk_format,
                provisioned_size=qemu_disk['virtual-size'],
                #                initial_size=int(props['populatedSize']),
                storage_domains=[
                    types.StorageDomain(
                        name=storage_domain
                    )
                ],
                bootable=boot,
            )
        )
        disks.append(new_disk)
#    for data in disks_metadata:
#        click.echo('Size: {} File: {}\n'.format(
#            data['virtual-size'], data['filename']))

#    for disk in ovf.iter("Disk"):

    vm = vms_service.add(
        types.Vm(
            cluster=types.Cluster(
                name=cluster,
            ),
            initialization=types.Initialization(
                configuration=types.Configuration(
                    type=types.ConfigurationType.OVA,
                    data=ovf_str
                )
            ),
        ),
    )

    logging.info('Restore of vm: {} complete'.format(vm.name))
    if debug:
        click.echo('Restore of vm: {} complete'.format(vm.name))
