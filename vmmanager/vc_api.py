# coding: utf-8

"""
Python program for VCenter Operations
"""
import time

from pyVmomi import vim

from vmmanager.models import *


def _refresh_vim_objs(model, content, *args, **kwargs):
    """
    update all VirtualMachine
    @param content: the service content of VCenter.
    @param related: weather to update the relationship between assets
    """
    typeMap = {
        vim.ComputeResource: ComputeResource,
        vim.ResourcePool: ResourcePool,
        vim.Network: Network,
        vim.Datastore: Datastore,
        vim.HostSystem: HostSystem,
        vim.VirtualMachine: VirtualMachine
    }
    if model not in typeMap.keys():
        return False
    vc = VCenter.objects.get(uuid=content.about.instanceUuid)
    container = content.rootFolder

    # discover or update model
    vimView = content.viewManager.CreateContainerView(container, [model], True)
    updList = []
    for obj in vimView.view:
        updList.append(obj._GetMoId())
        typeMap[model].create_or_update_by_vim(obj, vc, *args, **kwargs)
    # delete invalid objects
    qset = typeMap[model].objects.filter(vcenter=vc)
    if qset.count() > len(updList):
        for obj in qset:
            if obj.moid not in updList:
                print("Delete invalid obj: " + str(obj.moid))
                obj.delete()
    vimView.Destroy()
    return True


def get_obj(content, container=None, vimtype=None, name='', moid=''):
    """
    Return an object by name, if name is None the
    first found object is returned
    """
    if container == None:
        container = content.rootFolder
    objView = content.viewManager.CreateContainerView(container, vimtype, True)
    obj = None
    for c in objView.view:
        if name:
            if c.name == name:
                obj = c
                break
        elif moid:
            if c._GetMoId() == moid:
                obj = c
                break
        else:
            obj = c
            break

    objView.Destroy()
    return obj


def refresh_all_vms(content, related=False):
    """
    update all VirtualMachine
    @param content: the service content of VCenter.
    @param related: weather to update the relationship between assets
    """
    return _refresh_vim_objs(vim.VirtualMachine, content, related)


def refresh_all_assets(content, related=False):
    """
    update all assets including ComputeResource, ResourcePool, Network, Datastore, Hostsystem
    @param content: the service content of VCenter.
    @param related: weather to update the relationship between assets
    """
    successList = []
    if _refresh_vim_objs(vim.ComputeResource, content):
        successList.append('ComputeResource')

    if _refresh_vim_objs(vim.Network, content):
        successList.append('Network')

    if _refresh_vim_objs(vim.Datastore, content):
        successList.append('Datastore')

    if _refresh_vim_objs(vim.ResourcePool, content, related=related):
        successList.append('ResourcePool')

    if _refresh_vim_objs(vim.HostSystem, content, related=related):
        successList.append('HostSystem')


def _is_ipv4(ipstr):
    ipstr = str(ipstr)
    part = []
    if ipstr.count('.') == 2:
        if ipstr.split('.')[1]:
            return False
        part.append(int(ipstr.split('.')[0]))
        part.append(int(ipstr.split('.')[2]))
    elif ipstr.count('.') == 3:
        for i in range(4):
            part.append(int(ipstr.split('.')[i]))
    else:
        return False
    for i in part:
        if i < 0 or i > 255:
            return False
    return True


def refresh_ipusage(content):
    vc = VCenter.objects.get(uuid=content.about.instanceUuid)
    container = content.rootFolder
    ipusages = IPUsage.objects.all()
    # update ESXi Server IP Address
    hostlist = [host.name for host in HostSystem.objects.filter(vcenter=vc) if _is_ipv4(host.name)]
    for host in hostlist:
        qset = IPUsage.objects.filter(ipaddress=host)
        if qset.count() == 1:
            qset[0].manage('ESXi')

    # update VM IP Address
    vmlist = VirtualMachine.objects.filter(vcenter=vc)
    for vm in vmlist:
        vimobj = get_obj(content, container, [vim.VirtualMachine], moid=str(vm.moid))
        vm.update_ipusage(vimobj)


def wait_for_task(task):
    """ wait for a vCenter task to finish """
    task_done = False
    while not task_done:
        if task.info.state == 'success':
            return task.info.result

        if task.info.state == 'error':
            print "there was an error"
            task_done = True
        time.sleep(5)


def vim_vm_poweron(vim_vm, host=None):
    if isinstance(vim_vm, vim.VirtualMachine):
        try:
            if isinstance(host, vim.HostSystem):
                vim_vm.PowerOn(host)
            else:
                vim_vm.PowerOn()
            return True
        except:
            print("Can not power on the vm")


DNS_LIST = []
VM_TZ = "Asia/Shanghai"


def _vim_gen_spec_customize(is_windows, ipusage=None, hostname=None):
    custspec = vim.vm.customization.Specification
    # ipsettings
    if ipusage:
        ipsetting = vim.vm.customization.IPSettings()
        fixip = vim.vm.customization.FixedIp()
        fixip.ipAddress = ipusage.ipaddress
        ipsetting.ip = fixip
        network = ipusage.network
        ipsetting.subnetMask = network.netmask
        try:
            gateway = network.ipusage_set.get(used_manage_app='GW').ipaddress
        except:
            pass
        else:
            ipsetting.gateway = [gateway]
        if is_windows:
            ipsetting.dnsServerList = DNS_LIST
        ipadapter = vim.vm.customization.AdapterMapping()
        ipadapter.adapter = ipsetting
        custspec.nicSettingMap = [ipadapter]
    if hostname and not is_windows:
        linuxprep = vim.vm.customization.LinuxPrep()
        namegen = vim.vm.customization.FixedName()
        namegen.name = hostname
        linuxprep.hostName = namegen
        linuxprep.domain = hostname + '.site'
        linuxprep.hwClockUTC = False
        linuxprep.timeZone = VM_TZ
    return custspec


def _vim_set_customize(vim_vm, *args, **kwargs):
    try:
        guestos = vim_vm.guest.guestFamily
    except:
        print("Cannot get guest os type")
        return -1
    if 'win' in str(guestos).lower():
        is_windows = True
    else:
        is_windows = False
    vim_vm.Customize(_vim_gen_spec_customize(is_windows, *args, **kwargs))


def _vim_vm_clone(vim_src_vm, vm_name, vim_datastore, vim_resp, ipusage, power_on=False):
    try:
        guestos = vim_src_vm.guest.guestFamily
    except:
        print("Cannot get guest os type")
        return -1
    if 'win' in str(guestos).lower():
        is_windows = True
    else:
        is_windows = False
    vm_folder = vim_src_vm.parent
    while (isinstance(vm_folder.parent, vim.Folder)):
        vm_folder = vm_folder.parent
    # set relospec
    relospec = vim.vm.RelocateSpec()
    relospec.datastore = vim_datastore
    relospec.pool = vim_resp
    # set clonespec
    clonespec = vim.vm.CloneSpec()
    clonespec.location = relospec
    clonespec.powerOn = power_on
    clonespec.template = False
    clonespec.customization = _vim_gen_spec_customize(is_windows, ipusage, vm_name)
    # start clone task
    try:
        task = vim_src_vm.Clone(folder=vm_folder, name=vm_name, spec=clonespec)
    except:
        task = None
    return task, ""


def clone_vm(content, src_vm, vm_name, ipusage, datastore, cluster=None, resourcepool=None, power_on=False):
    task = None
    errmsg = ""
    if not isinstance(src_vm, VirtualMachine):
        errmsg = "Src_vm not a VirtualMachine instance"
        return task, errmsg
    if src_vm.vcenter.uuid != content.about.instanceUuid:
        errmsg = "Src_vm not belong to the connected vcenter"
        return task, errmsg
    # Get vim_vm_obj
    vim_src_vm = get_obj(content, vimtype=[vim.VirtualMachine], moid=str(src_vm.moid))
    # Get vim_datastore_obj
    if not datastore:
        datastore = src_vm.datastores[0]
    freespace = datastore.free_space_mb if datastore.free_space_mb else 0
    if freespace < src_vm.storage_mb * 2:
        errmsg = "Datastore not enough space!"
        return task, errmsg
    vim_datastore = get_obj(content, vimtype=[vim.Datastore], moid=str(datastore.moid))
    # Get vim_resp_obj
    if resourcepool:
        if not isinstance(resourcepool, ResourcePool):
            errmsg = "resourcepool not a ResourcePool instance"
            return task, errmsg
        vim_resourcepool = get_obj(content, vimtype=[vim.ResourcePool], moid=str(resourcepool.moid))
    elif cluster:
        if not isinstance(resourcepool, ResourcePool):
            errmsg = "cluster not a ComputeResource instance"
            return task, errmsg
        try:
            resourcepool = cluster.resourcepool_set.get(parent__isnull=True)
        except:
            errmsg = "Cannot allocate resource pool"
            return task, errmsg
        vim_resourcepool = get_obj(content, vimtype=[vim.ResourcePool], moid=str(resourcepool.moid))
    else:
        errmsg = "Neither Cluster nor ResourcePool is given as a parameter"
        return task, errmsg
    return _vim_vm_clone(vim_src_vm, vm_name, vim_datastore, vim_resourcepool, ipusage, power_on)


def _vim_gen_spec_disk(disk_size, unit_number, controller_key, thin_disk=False):
    new_disk_kb = int(disk_size) * 1024 * 1024
    disk_spec = vim.vm.device.VirtualDeviceSpec()
    disk_spec.fileOperation = "create"
    disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
    disk_spec.device = vim.vm.device.VirtualDisk()
    disk_spec.device.backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
    disk_spec.device.backing.thinProvisioned = thin_disk
    disk_spec.device.backing.diskMode = 'persistent'
    disk_spec.device.unitNumber = unit_number
    disk_spec.device.capacityInKB = new_disk_kb
    disk_spec.device.controllerKey = controller_key
    return disk_spec


def vim_vm_reconfig(vim_vm, tg_annotation='', tg_cpu_num=-1, tg_cpu_cores=-1, tg_mem_mb=-1, tg_datadisk_gb=-1):
    configspec = vim.vm.ConfigSpec
    if tg_annotation:
        configspec.annotation = tg_annotation
    if tg_cpu_num > 0:
        configspec.numCPUs = tg_cpu_num
    if tg_cpu_cores > 0:
        configspec.numCoresPerSocket = tg_cpu_cores
    if tg_mem_mb > 0:
        configspec.memoryMB = tg_mem_mb
    if tg_datadisk_gb > 0:
        # get all disks on a VM, set unit_number to the next available, and get controller
        controller_key = None
        unit_number = 0
        for dev in vim_vm.config.hardware.device:
            if hasattr(dev.backing, 'fileName'):
                unit_number = int(dev.unitNumber) + 1
                # unit_number 7 reserved for scsi controller
                if unit_number == 7:
                    unit_number += 1
            if isinstance(dev, vim.vm.device.VirtualSCSIController):
                controller_key = dev.key
        disk_spec = _vim_gen_spec_disk(tg_datadisk_gb, unit_number, controller_key)
        configspec.deviceChange = [disk_spec]
    return vim_vm.Reconfigure(spec=configspec), ""


def reconfig_vm(content, vm, tg_annotation='', tg_cpu_num=-1, tg_cpu_cores=-1, tg_mem_mb=-1, tg_datadisk_gb=-1):
    task = None
    errmsg = ""
    if not isinstance(vm, VirtualMachine):
        errmsg = "Src_vm not a VirtualMachine instance"
        return task, errmsg
    if vm.vcenter.uuid != content.about.instanceUuid:
        errmsg = "Src_vm not belong to the connected vcenter"
        return task, errmsg
    # Get vim_vm_obj
    vim_vm = get_obj(content, vimtype=[vim.VirtualMachine], moid=str(vm.moid))
    return vim_vm_reconfig(vim_vm, tg_annotation, tg_cpu_num, tg_cpu_cores, tg_mem_mb, tg_datadisk_gb)


def get_cluster(env_type):
    pass