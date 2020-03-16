import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import click
import ovirtsdk4 as sdk
import ovirtsdk4.types as types

import helpers

FORMAT = '%(asctime)s %(levelname)s %(message)s'
AgentVM = 'backuprestore'
Description = 'cli-ovirt-backup'
VERSION = '0.5.2'


def print_version(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    click.echo(VERSION)
    ctx.exit()


@click.group()
@click.option('--version', '-v', is_flag=True, callback=print_version, expose_value=False, is_eager=True)
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
    '--api', '-a', envvar='OVIRTURL', required=True, help='url for oVirt API https://manager.example.com/ovirt-engine/api'
)
@click.option(
    '--backup-path', '-b', envvar='BACKUPPATH', type=click.Path(), default='/ovirt-backup', show_default=True, help='path of backups'
)
@click.option(
    '--log', '-l', envvar='OVIRTLOG', type=click.Path(), default='/var/log/cli-ovirt-backup.log', show_default=True, help='path log file'
)
@click.option('--debug', '-d', is_flag=True, default=False, help='debug mode')
@click.option('--unarchive', '-n', is_flag=True, default=False, help='archive backup')
def backup(username, password, ca, vmname, api, debug, backup_path, log, unarchive):
    logging.basicConfig(level=logging.DEBUG, format=FORMAT,
                        filename=log)
    connection = sdk.Connection(
        url=api,
        username=username,
        password=password,
        ca_file=ca,
        debug=debug,
        log=logging.getLogger(),
    )

    # id for event in virt manager
    event_id = int(time.time())

    logging.info('[{}] Connected to the server.'.format(event_id))
    if debug:
        click.echo('[{}] Connected to the server.'.format(event_id))

    # Get the reference to the root of the services tree:
    system_service = connection.system_service()

    # Get the reference to the service that we will use to send events to
    # the audit log:
    events_service = system_service.events_service()

    # Get the reference to the service that manages the virtual machines:
    vms_service = system_service.vms_service()

    vm = helpers.vmobj(vms_service, vmname)
    ts = str(event_id)

    message = (
        '[{}] Backup of virtual machine \'{}\' using snapshot \'{}\' is '
        'starting.'.format(event_id, vm.name, Description)
    )
    helpers.send_events(events_service, event_id,
                        types, Description, message, vm)

    timestamp = time.strftime("%Y%m%d%H%M%S")
    backup_path_obj = Path(backup_path)
    backup_name_obj = Path(vmname + '-' + timestamp)
    vm_backup_obj = backup_path_obj / backup_name_obj
    vm_backup_absolute = vm_backup_obj.absolute().as_posix()

    logging.info(
        '[{}] Found data virtual machine \'{}\', the id is \'{}\'.'.format(
            event_id, vm.name, vm.id)
    )
    if debug:
        click.echo(
            '[{}] Found data virtual machine \'{}\', the id is \'{}\'.'.format(event_id, vmname, vm.id))
    vmAgent = helpers.vmobj(vms_service, AgentVM)
    logging.info(
        '[{}] Found agent virtual machine \'{}\', the id is \'{}\'.'.format(event_id,
                                                                            vmAgent.name, vmAgent.id)
    )
    if debug:
        click.echo(
            '[{}] Found agent virtual machine \'{}\', the id is \'{}\'.'.format(event_id, vmAgent.name, vmAgent.id))

    helpers.createdir(vm_backup_absolute)
    logging.info('[{}] Creating directory {}.'.format(
        event_id, vm_backup_absolute))
    if debug:
        click.echo('[{}] Creating directory {}.'.format(
            event_id, vm_backup_absolute))
    # Find the services that manage the data and agent virtual machines:
    data_vm_service = vms_service.vm_service(vm.id)
    agent_vm_service = vms_service.vm_service(vmAgent.id)

    ovf_file = helpers.writeconfig(vm, vm_backup_absolute + '/')
    logging.info('[{}] Wrote OVF to file \'{}\''.format(event_id, ovf_file))
    if debug:
        click.echo('[{}] Wrote OVF to file \'{}\''.format(event_id, ovf_file))

    snaps_service = data_vm_service.snapshots_service()

    snap = helpers.createsnapshot(snaps_service, types, Description)
    logging.info('[{}] Sent request to create snapshot \'{}\', the id is \'{}\'.'.format(
        event_id, snap.description, snap.id))
    if debug:
        click.echo('[{}] Sent request to create snapshot \'{}\', the id is \'{}\'.'.format(
            event_id, snap.description, snap.id))

    snap_service = snaps_service.snapshot_service(snap.id)
    helpers.waitingsnapshot(snap, types, logging, time,
                            snap_service, click, debug, event_id)

    # Retrieve the descriptions of the disks of the snapshot:
    snap_disks_service = snap_service.disks_service()
    snap_disks = snap_disks_service.list()

    # Attach disk service
    attachments_service = agent_vm_service.disk_attachments_service()

    attachments = helpers.populateattachments(
        snap_disks, snap, attachments_service, types, logging, click, debug)

    for attach in attachments:
        logging.info(
            '[{}] Attached disk \'{}\' to the agent virtual machine.'.format(
                event_id, attach.disk.id)
        )
        if debug:
            click.echo(
                '[{}] Attached disk \'{}\' to the agent virtual machine.'.format(
                    event_id, attach.disk.id)
            )

    block_devices = helpers.getdevices()
    devices = {}
    for i in range(len(attachments)):
        devices[attachments[i].disk.id] = '/dev/' + block_devices[i]

    helpers.converttoqcow2(devices, vm_backup_absolute +
                           '/', debug, logging, click)

    for attach in attachments:
        attachment_service = attachments_service.attachment_service(attach.id)
        attachment_service.remove()
        logging.info(
            '[{}] Detached disk \'{}\' to from the agent virtual machine.'.format(event_id, attach.disk.id))
        if debug:
            click.echo(
                '[{}] Detached disk \'{}\' to from the agent virtual machine.'.format(
                    event_id, attach.disk.id)
            )
    # Remove the snapshot:
    snap_service.remove()
    logging.info('[{}] Removed the snapshot \'{}\'.'.format(
        event_id, snap.description))
    if debug:
        click.echo('[{}] Removed the snapshot \'{}\'.'.format(
            event_id, snap.description))

    if not unarchive:
        logging.info('[{}] Archiving \'{}\' in \'{}.tar.gz\''.format(
            event_id, vm_backup_absolute, vm_backup_absolute))
        # making archiving
        helpers.make_archive(backup_path, vm_backup_absolute)
        shutil.rmtree(vm_backup_absolute)
        if debug:
            click.echo('[{}] Archiving \'{}\' in \'{}.tar.gz\''.format(
                event_id, vm_backup_absolute, vm_backup_absolute))
    event_id += 1
    message = (
        '[{}] Backup of virtual machine \'{}\' using snapshot \'{}\' is '
        'completed.'.format(event_id, vm.name, Description)
    )
    helpers.send_events(events_service, event_id,
                        types, Description, message, vm)

    # Finish the connection to the VM Manager
    connection.close()
    logging.info('[{}] Disconnected to the server.'.format(event_id))
    if debug:
        click.echo('[{}] Disconnected to the server.'.format(event_id))


@cli.command()
@click.argument('file')
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
    '--api', '-a', envvar='OVIRTURL', required=True, help='url for oVirt API https://manager.example.com/ovirt-engine/api'
)
@click.option(
    '--storage-domain', '-s', envvar='OVIRTSD', required=True, help='Name of oVirt/RHV Storage Domain'
)
@click.option(
    '--cluster', '-C', envvar='OVIRTCLUSTER', required=True, help='Name of oVirt/RHV Cluster'
)
@click.option(
    '--log', '-l', envvar='OVIRTLOG', type=click.Path(), default='/var/log/cli-ovirt-restore.log', show_default=True, help='path log file'
)
@click.option('--debug', '-d', is_flag=True, default=False, help='debug mode')
def restore(username, password, file, ca, api, storage_domain, log, debug, cluster):

    logging.basicConfig(level=logging.DEBUG, format=FORMAT,
                        filename=log)
    connection = sdk.Connection(
        url=api,
        username=username,
        password=password,
        ca_file=ca,
        debug=debug,
        log=logging.getLogger(),
    )

    # id for event in virt manager
    event_id = int(time.time())

    logging.info('[{}] Connected to the server.'.format(event_id))
    if debug:
        click.echo('[{}] Connected to the server.'.format(event_id))

    # Get the reference to the root of the services tree:
    system_service = connection.system_service()

    # Get the reference to the service that we will use to send events to
    # the audit log:
    events_service = system_service.events_service()

    disks_service = system_service.disks_service()

    vms_service = system_service.vms_service()

    p = Path(file)

    # Get absolute path of restore "file" variable
    tar_file = p.absolute().as_posix()
    # Get full path of parent related to "file" variable
    parent_path = p.absolute().parent.as_posix()

    basedir = tar_file.split('.', 2)[0]
    xml_file = ''

    basedir_obj = Path(basedir)

    vm_name = re.sub(r"\-.*$", '', basedir_obj.name)

    vm = helpers.vmobj(vms_service, vm_name)
    if vm:
        logging.info('[{}] vm {} alredy exists'.format(event_id, vm_name))
        if debug:
            click.echo('[{}] vm {} alredy exists'.format(event_id, vm_name))

    message = (
        '[{}] Restore of virtual machine \'{}\' using file \'{}\' is '
        'starting.'.format(event_id, vm_name, tar_file)
    )

    logging.info(message)
    if debug:
        click.echo(message)

    helpers.send_events(events_service, event_id,
                        types, Description, message)

    if not basedir_obj.exists():
        logging.info("[{}] File {} is compressed".format(event_id, tar_file))
        if debug:
            click.echo("[{}] File {} is compressed".format(event_id, tar_file))
        # Getting name of extracted directory
        logging.info('[{}] Init descompress'.format(event_id))
        if debug:
            click.echo('[{}] Init descompress'.format(event_id))
        #helpers.unpack_archive(tar_file, basedir_obj)
        helpers.unpack_archive(tar_file, parent_path)
        logging.info('[{}] Finish decompress'.format(event_id))
        if debug:
            click.echo('[{}] Finish decompress'.format(event_id))

    # Get the reference to the service that manages the virtual machines:
    vms_service = system_service.vms_service()
    if basedir_obj.exists():
        for f in basedir_obj.glob('**/*.ovf'):
            xml_file = Path(f).absolute().as_posix()
        logging.info('[{}] Configuration file is [{}]'.format(
            event_id, xml_file))
        if debug:
            click.echo('[{}] Configuration file is [{}]'.format(
                event_id, xml_file))
    else:
        logging.info('failed to decompress')
        exit(1)

    ovf, ovf_str = helpers.ovf_parse(xml_file)

    disks = []
    namespace = '{http://schemas.dmtf.org/ovf/envelope/1/}'

    metadata = []
    metas = {}
    elements = ["boot", "volume-format", "diskId",
                "disk-alias", "disk-description", "size", "fileRef"]

    logging.info('[{}] Extracting ovf data'.format(event_id))
    if debug:
        click.echo('[{}] Extracting ovf data'.format(event_id))
    for disk in ovf.iter('Disk'):
        for element in elements:
            if element == 'size':
                metas[str(element)] = int(disk.get(namespace+element)) * 2**30
            elif element == 'fileRef':
                metas[str(element)] = str(
                    disk.get(namespace+element)).split("/")[0]
                metas[str(element)+'_image'] = str(
                    disk.get(namespace+element)).split("/")[1]
            else:
                metas[str(element)] = disk.get(namespace+element)
        metadata.append(metas.copy())

    logging.info('[{}] Defining disks'.format(event_id))
    if debug:
        click.echo('[{}] Defining disks'.format(event_id))
    for meta in metadata:
        logging.info('[{}] Defining disk {} with image {} and size {}'.format(event_id,
                                                                              meta['fileRef'], meta['fileRef_image'], meta['size']))

        if debug:
            click.echo('[{}] Defining disk {}'.format(
                event_id, meta['fileRef']))
        if meta['volume-format'] == 'COW':
            disk_format = types.DiskFormat.COW
        else:
            disk_format = types.DiskFormat.RAW
        if meta['boot']:
            boot = True
        new_disk = disks_service.add(
            disk=types.Disk(
                id=meta['fileRef'],
                name=meta['disk-alias'],
                description=meta['disk-description'],
                format=disk_format,
                provisioned_size=meta['size'],
                storage_domains=[
                    types.StorageDomain(name=storage_domain)
                ],
                bootable=boot,
                image_id=meta['fileRef_image']
            )
        )

        disk_service = disks_service.disk_service(new_disk.id)
        while disk_service.get().status != types.DiskStatus.OK:
            time.sleep(5)
            logging.info('[{}] Waiting till the disk is created, the satus is \'{}\'.'.format(event_id,
                                                                                              disk_service.get().status))
            if debug:
                click.echo('[{}] Waiting till the disk is created, the satus is \'{}\'.'.format(event_id,
                                                                                                disk_service.get().status))
        disks.append(new_disk)

    vm = vms_service.add(
        types.Vm(
            cluster=types.Cluster(
                name=cluster,
            ),
            initialization=types.Initialization(
                configuration=types.Configuration(
                    type=types.ConfigurationType.OVF,
                    data=ovf_str
                )
            ),
        ),
    )

    logging.info('[{}] Restore of vm: {} complete'.format(event_id, vm.name))
    if debug:
        click.echo('[{}] Restore of vm: {} complete'.format(event_id, vm.name))
