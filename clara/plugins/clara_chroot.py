#!/usr/bin/env python
# -*- coding: utf-8 -*-
##############################################################################
#  Copyright (C) 2016 EDF SA                                                 #
#                                                                            #
#  This file is part of Clara                                                #
#                                                                            #
#  This software is governed by the CeCILL-C license under French law and    #
#  abiding by the rules of distribution of free software. You can use,       #
#  modify and/ or redistribute the software under the terms of the CeCILL-C  #
#  license as circulated by CEA, CNRS and INRIA at the following URL         #
#  "http://www.cecill.info".                                                 #
#                                                                            #
#  As a counterpart to the access to the source code and rights to copy,     #
#  modify and redistribute granted by the license, users are provided only   #
#  with a limited warranty and the software's author, the holder of the      #
#  economic rights, and the successive licensors have only limited           #
#  liability.                                                                #
#                                                                            #
#  In this respect, the user's attention is drawn to the risks associated    #
#  with loading, using, modifying and/or developing or reproducing the       #
#  software by the user in light of its specific status of free software,    #
#  that may mean that it is complicated to manipulate, and that also         #
#  therefore means that it is reserved for developers and experienced        #
#  professionals having in-depth computer knowledge. Users are therefore     #
#  encouraged to load and test the software's suitability as regards their   #
#  requirements in conditions enabling the security of their systems and/or  #
#  data to be ensured and, more generally, to use and operate it in the      #
#  same conditions as regards security.                                      #
#                                                                            #
#  The fact that you are presently reading this means that you have had      #
#  knowledge of the CeCILL-C license and that you accept its terms.          #
#                                                                            #
##############################################################################
"""
Creates and updates a chroot.

Usage:
    clara chroot create <dist> [<chroot_dir>] [--keep-chroot-dir]
    clara chroot edit <dist> [<chroot_dir>]
    clara chroot install <dist> [<packages>]
    clara chroot remove <dist> [<packages>]
    clara chroot -h | --help | help

"""

import errno
import logging
import os
import pty
import shutil
import atexit
import subprocess
import sys
import time

import docopt
from clara.utils import clara_exit, run, get_from_config, conf


def run_chroot(cmd):
    logging.debug("chroot/run_chroot: {0}".format(" ".join(cmd)))

    try:
        retcode = subprocess.call(cmd)
    except OSError, e:
        if (e.errno == errno.ENOENT):
            clara_exit("Binary not found, check your path and/or retry as root. \
                      You were trying to run:\n {0}".format(" ".join(cmd)))

    if retcode != 0:
        umount_chroot()
        if not keep_chroot_dir:
            shutil.rmtree(work_dir)
        clara_exit(' '.join(cmd))


def base_install():
    # Debootstrap
    apt_pref = work_dir + "/etc/apt/preferences.d/00custompreferences"
    apt_conf = work_dir + "/etc/apt/apt.conf.d/99nocheckvalid"
    dpkg_conf = work_dir + "/etc/dpkg/dpkg.cfg.d/excludes"
    etc_host = work_dir + "/etc/hosts"

    debiandist = get_from_config("chroot", "debiandist", dist)
    debmirror = get_from_config("chroot", "debmirror", dist)

    if conf.ddebug:
        run(["debootstrap", "--verbose", debiandist, work_dir, debmirror])
    else:
        run(["debootstrap", debiandist, work_dir, debmirror])

    # Prevent services from starting automatically
    policy_rc = work_dir + "/usr/sbin/policy-rc.d"
    with open(policy_rc, 'w') as p_rcd:
        p_rcd.write("exit 101")
    p_rcd.close()
    os.chmod(work_dir + "/usr/sbin/policy-rc.d", 0o755)

    # Mirror setup
    list_repos = get_from_config("chroot", "list_repos", dist).split(",")
    with open(src_list, 'w') as fsources:
        for line in list_repos:
            fsources.write(line + '\n')

    with open(apt_pref, 'w') as fapt:
        fapt.write("""Package: *
Pin: release o={0}
Pin-Priority: 5000

Package: *
Pin: release o={1}
Pin-Priority: 6000
""".format(dist, get_from_config("common", "origin", dist)))

    # Misc config
    with open(apt_conf, 'w') as fconf:
        fconf.write('Acquire::Check-Valid-Until "false";\n')

    lists_hosts = get_from_config("chroot", "etc_hosts", dist).split(",")
    with open(etc_host, 'w') as fhost:
        for elem in lists_hosts:
            if ":" in elem:
                ip, host = elem.split(":")
                fhost.write("{0} {1}\n".format(ip, host))
            else:
                logging.warning("The option etc_hosts is malformed or missing an argument")

    with open(dpkg_conf, 'w') as fdpkg:
        fdpkg.write("""# Drop locales except French
path-exclude=/usr/share/locale/*
path-include=/usr/share/locale/fr/*
path-include=/usr/share/locale/locale.alias

""")

    # Set root password to 'clara'
    part1 = subprocess.Popen(["echo", "root:clara"], stdout=subprocess.PIPE)
    part2 = subprocess.Popen(["chroot", work_dir, "/usr/sbin/chpasswd"], stdin=part1.stdout)
    part1.stdout.close()  # Allow part1 to receive a SIGPIPE if part2 exits.
    # output = part2.communicate()[0]


def mount_chroot():
    run(["chroot", work_dir, "mount", "-t", "proc", "none", "/proc"])
    run(["chroot", work_dir, "mount", "-t", "sysfs", "none", "/sys"])
    try:
        extra_bind_mounts = get_from_config("chroot", "extra_bind_mounts", dist).split(",")
    except:
        extra_bind_mounts = None
    if not extra_bind_mounts:
        logging.warning("extra_bind_mounts is not specified in config.ini")
    else:
        for mounts_params in extra_bind_mounts:
            dirtomount = mounts_params.split(" ")[0]
            mountpoint = work_dir+mounts_params.split(" ")[1]
            if not os.path.isdir(mountpoint):
                os.makedirs(mountpoint)
            run(["mount", "-o", "bind", dirtomount, mountpoint])


def umount_chroot():
    if os.path.ismount(work_dir + "/proc/sys/fs/binfmt_misc"):
        run(["chroot", work_dir, "umount", "/proc/sys/fs/binfmt_misc"])

    if os.path.ismount(work_dir + "/sys"):
        run(["chroot", work_dir, "umount", "/sys"])

    if os.path.ismount(work_dir + "/proc"):
        run(["chroot", work_dir, "umount", "/proc"])
    try:
        extra_bind_mounts = get_from_config("chroot", "extra_bind_mounts", dist).split(",")
    except:
        extra_bind_mounts = None
    if not extra_bind_mounts:
        logging.warning("extra_bind_mounts is not specified in config.ini")
    else:
        for mounts_params in extra_bind_mounts:
            mountpoint = work_dir+mounts_params.split(" ")[1]
            if os.path.ismount(mountpoint):
                run(["umount", mountpoint])
    time.sleep(1)  # Wait one second so the system has time to unmount
    with open("/proc/mounts", "r") as file_to_read:
        for line in file_to_read:
            if work_dir in line:
                clara_exit("Something went wrong when umounting in the chroot")


def system_install():
    mount_chroot()
    run_chroot(["chroot", work_dir, "apt-get", "update"])

    # Install packages from package_file if this file has been set in config.ini
    try:
        package_file = get_from_config("chroot", "package_file", dist)
    except:
        package_file = None

    if not package_file:
        logging.warning("package_file is not specified in config.ini")
    elif not os.path.isfile(package_file):
        logging.warning("package_file contains '{0}' and it is not a file.".format(package_file))
    else:
        shutil.copy(package_file, work_dir + "/tmp/packages.file")
        for i in range(0, 2):
            part1 = subprocess.Popen(["cat", work_dir + "/tmp/packages.file"],
                                     stdout=subprocess.PIPE)
            part2 = subprocess.Popen(["chroot", work_dir, "dpkg", "--set-selections"],
                                     stdin=part1.stdout, stdout=subprocess.PIPE)
            part1.stdout.close()  # Allow part1 to receive a SIGPIPE if part2 exits.
            output = part2.communicate()[0]
            run_chroot(["chroot", work_dir, "apt-get", "dselect-upgrade", "-u", "--yes", "--force-yes"])

    # Set presseding if the file has been set in config.ini
    preseed_file = get_from_config("chroot", "preseed_file", dist)
    if not os.path.isfile(preseed_file):
        logging.warning("preseed_file contains '{0}' and it is not a file!".format(preseed_file))
    else:
        shutil.copy(preseed_file, work_dir + "/tmp/preseed.file")
        # we need to install debconf-utils
        run_chroot(["chroot", work_dir, "apt-get", "install", "--no-install-recommends", "--yes", "--force-yes", "debconf-utils"])
        run_chroot(["chroot", work_dir, "apt-get", "update"])
        run_chroot(["chroot", work_dir, "/usr/lib/dpkg/methods/apt/update", "/var/lib/dpkg/"])
        run_chroot(["chroot", work_dir, "debconf-set-selections", "/tmp/preseed.file"])

    # Install extra packages if extra_packages has been set in config.ini
    extra_packages = get_from_config("chroot", "extra_packages", dist)
    if len(extra_packages) == 0:
        logging.warning("extra_packages hasn't be set in the config.ini")
    else:
        run_chroot(["chroot", work_dir, "apt-get", "update"])
        pkgs = extra_packages.split(",")
        run_chroot(["chroot", work_dir, "apt-get", "install", "--no-install-recommends", "--yes", "--force-yes"] + pkgs)

    run_chroot(["chroot", work_dir, "apt-get", "clean"])
    umount_chroot()


def install_files():
    list_files_to_install = get_from_config("chroot", "list_files_to_install", dist)
    if not os.path.isfile(list_files_to_install):
        logging.warning("{0} is not a file!".format(list_files_to_install))

    else:
        dir_origin = get_from_config("chroot", "dir_files_to_install", dist)
        if not os.path.isdir(dir_origin):
            logging.warning("{0} is not a directory!".format(dir_origin))

        with open(list_files_to_install, "r") as file_to_read:
            for line in file_to_read:
                orig, dest, perm = line.rstrip().split()
                path_orig = dir_origin + "/" + orig
                path_dest = work_dir + "/" + dest
                file_perm = int(perm, 8)  # tell int to use base 8
                final_file = path_dest + orig

                if not os.path.isfile(path_orig):
                    logging.warning("{0} is not a file!".format(path_orig))

                if not os.path.isdir(path_dest):
                    os.makedirs(path_dest)
                shutil.copy(path_orig, path_dest)
                os.chmod(final_file, file_perm)

                if ("etc/init.d" in dest):
                    run_chroot(["chroot", work_dir, "update-rc.d", orig, "defaults"])

    # Empty hostname
    os.remove(work_dir + "/etc/hostname")
    run_chroot(["chroot", work_dir, "touch", "/etc/hostname"])


def install_https_apt():
    try:
        list_https_repos = get_from_config("chroot", "list_https_repos", dist).split(",")
    except:
        list_https_repos = None
    if not list_https_repos:
        logging.warning("list_https_repos is not specified in config.ini")
    else:
        # Install https transport for apt
        run_chroot(["chroot", work_dir, "apt-get", "update"])
        run_chroot(["chroot", work_dir, "apt-get", "install", "--no-install-recommends", "--yes", "--force-yes", "apt-transport-https", "openssl"])
        # Add https sources in apt config
        with open(src_list, 'a') as fsources:
            for line in list_https_repos:
                fsources.write(line + '\n')
        # Add ssl keys
        apt_ssl_key_source = get_from_config("chroot", "apt_ssl_key", dist)
        apt_ssl_crt_source = get_from_config("chroot", "apt_ssl_crt", dist)
        path_dest = "/etc/ssl/"
        apt_ssl_key = path_dest+"private/"+os.path.basename(apt_ssl_key_source)
        apt_ssl_crt = path_dest+"certs/"+os.path.basename(apt_ssl_crt_source)
        if not os.path.isfile(apt_ssl_key_source):
            logging.warning("{0} is not a file!".format(apt_ssl_key_source))
        if not os.path.isfile(apt_ssl_crt_source):
            logging.warning("{0} is not a file!".format(apt_ssl_crt_source))
        if not os.path.isdir(path_dest):
            os.makedirs(path_dest)
        shutil.copy(apt_ssl_key_source, work_dir+apt_ssl_key)
        os.chmod(apt_ssl_key, 0600)
        shutil.copy(apt_ssl_crt_source, work_dir+apt_ssl_crt)
        os.chmod(apt_ssl_crt, 0644)
        # Add apt config for ssl key
        with open(work_dir+"/etc/apt/apt.conf.d/52ssl", 'w') as apt_conf_ssl:
            apt_conf_ssl.write("""#
# Configuration for apt over https, for secured repository
#
Acquire::https::SslCert "{0}";
Acquire::https::SslKey  "{1}";
Acquire::https::Verify-Peer "false";
""".format(apt_ssl_crt, apt_ssl_key))
        # Finally update packages database
        run_chroot(["chroot", work_dir, "apt-get", "update"])


def remove_files():
    files_to_remove = get_from_config("chroot", "files_to_remove", dist).split(',')
    for f in files_to_remove:
        if os.path.isfile(work_dir + "/" + f):
            os.remove(work_dir + "/" + f)
    os.remove(work_dir + "/usr/sbin/policy-rc.d")


def run_script_post_creation():
    script = get_from_config("chroot", "script_post_creation", dist)
    if len(script) == 0:
        logging.warning("script_post_creation hasn't be set in the config.ini")
    elif not os.path.isfile(script):
        logging.warning("File {0} not found!".format(script))
    else:
        # Copy the script into the chroot and make sure it's executable
        shutil.copy(script, work_dir + "/tmp/script")
        os.chmod(work_dir + "/tmp/script", 0o755)
        run_chroot(["chroot", work_dir, "bash", "/tmp/script"])


def edit(chroot):
    if (chroot is None):
        chroot_dir = work_dir
    else:
        chroot_dir = chroot

    if not os.path.isdir(chroot_dir):
        clara_exit("The directory {0} doesn't exist.".format(chroot_dir))

    # Work in the chroot
    mount_chroot()
    os.chdir(work_dir)
    logging.info("Entering into the chroot to edit. ^d when you have finished.")
    os.putenv("PROMPT_COMMAND", "echo -ne  '\e[1;31m({0}) clara chroot> \e[0m'".format(dist))
    pty.spawn(["chroot", "."])

    save = raw_input('Save changes made in the chroot ? (N/y)')
    logging.debug("Input from the user: '{0}'".format(save))
    if save not in ('Y', 'y'):
        clara_exit("Changes ignored. The chroot {0} hasn't been modified.".format(chroot_dir))

    os.chmod(chroot_dir, 0o755)


def clean_and_exit():
    if os.path.exists(work_dir):
        umount_chroot()
        if not keep_chroot_dir:
            shutil.rmtree(work_dir)


def install_packages(packages):
    if len(packages) == 0:
        logging.warning("No package list provided")
    else:
        pkgs = packages.split(',')
        mount_chroot
        run_chroot(["chroot", work_dir, "apt-get", "install", "--no-install-recommends", "--yes", "--force-yes"] + pkgs)


def remove_packages(packages):
    if len(packages) == 0:
        logging.warning("No package list provided")
    else:
        pkgs = packages.split(',')
        mount_chroot
        run_chroot(["chroot", work_dir, "apt-get", "remove", "--yes", "--force-yes"] + pkgs)


def main():
    logging.debug(sys.argv)
    dargs = docopt.docopt(__doc__)

    global work_dir, keep_chroot_dir, src_list, dist
    dist = get_from_config("common", "default_distribution")
    if dargs['<dist>'] is not None:
        dist = dargs["<dist>"]
    if dist not in get_from_config("common", "allowed_distributions"):
        clara_exit("{0} is not a know distribution".format(dist))
    work_dir = get_from_config("chroot", "trg_dir", dist)

    src_list = work_dir + "/etc/apt/sources.list"
    keep_chroot_dir = True
    # Not executed in the following cases
    # - the program dies because of a signal
    # - os._exit() is invoked directly
    # - a Python fatal error is detected (in the interpreter)
    atexit.register(clean_and_exit)

    if dargs['create']:
        base_install()
        system_install()
        install_files()
        install_https_apt()
        remove_files()
        run_script_post_creation()
    elif dargs['edit']:
        edit(dargs['<chroot_dir>'])
    elif dargs['install']:
        install_packages(dargs['<packages>'])
    elif dargs['remove']:
        remove_packages(dargs['<packages>'])


if __name__ == '__main__':
    main()