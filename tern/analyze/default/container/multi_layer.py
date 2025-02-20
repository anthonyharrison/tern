# -*- coding: utf-8 -*-
#
# Copyright (c) 2019-2021 VMware, Inc. All Rights Reserved.
# SPDX-License-Identifier: BSD-2-Clause

"""
Functions to analyze the subsequent layers in default mode
"""

import glob
import logging
import os

from tern.report import errors
from tern.utils import constants
from tern.utils import rootfs
from tern.classes.notice import Notice
from tern.analyze import common
from tern.report import formats
from tern.analyze.default import default_common as dcom
from tern.analyze.default import core
from tern.analyze.default.dockerfile import lock
from tern.analyze.default.command_lib import command_lib


# global logger
logger = logging.getLogger(constants.logger_name)


def mount_overlay_fs(image_obj, top_layer, driver):
    '''Given the image object and the top most layer, mount all the layers
    until the top layer using overlayfs'''
    tar_layers = []
    for index in range(0, top_layer + 1):
        tar_layers.append(image_obj.layers[index].tar_file)
    target = rootfs.mount_diff_layers(tar_layers, driver)
    return target


def apply_layers(image_obj, top_layer):
    """Apply image diff layers without using a kernel snapshot driver"""
    # All merging happens in the merge directory
    target = os.path.join(rootfs.get_working_dir(), constants.mergedir)
    layer_dir = rootfs.get_untar_dir(image_obj.layers[top_layer].tar_file)
    layer_contents = layer_dir + '/*'
    # Account for whiteout files
    for fd in image_obj.layers[top_layer].files:
        if fd.is_whiteout:
            # delete the corresponding file or directory in the target
            # directory as well as the layer contents directory
            deleted = fd.name.replace('.wh.', '')
            delpath = os.path.join(target, os.path.dirname(fd.path), deleted)
            if os.path.exists(delpath):
                if os.path.isfile(delpath):
                    rootfs.root_command(['rm'], delpath)
                else:
                    rootfs.root_command(rootfs.remove, delpath)
                os.remove(os.path.join(layer_dir, fd.path))
    # Finally, bulk copy the layer contents into the target directory
    # if there are any files to move
    if os.listdir(layer_dir):
        rootfs.root_command(['cp', '-r'] + glob.glob(layer_contents), target)
    return target


def prep_layers(image_obj, top_layer, driver='default'):
    """Prepare the layer diff contents uptil the required point in time
    based on the driver"""
    if driver == 'default':
        return apply_layers(image_obj, top_layer)
    return mount_overlay_fs(image_obj, top_layer, driver)


def fresh_analysis(image_obj, curr_layer, prereqs, options):
    """This is a subroutine that is run if there is no chached results or if
    the user wants to redo the analysis
    1 Check if we have a shell, if not then see if we can find a shell
    2 Check if we can find any info in the container history (created_by)
    3 If this exists then check if we can parse the command to figure out
      what package managers were used.
    4 Use the prescribed methods for the package managers to retrieve
    """
    # set up a notice origin for the current layer
    origin_curr_layer = 'Layer {}'.format(
        image_obj.layers[curr_layer].layer_index)
    image_obj.layers[curr_layer].origins.add_notice_to_origins(
        origin_curr_layer, Notice(formats.layer_created_by.format(
            created_by=image_obj.layers[curr_layer].created_by), 'info'))
    # if there is no shell, try to see if it exists in the current layer
    if not prereqs.fs_shell:
        prereqs.fs_shell = dcom.get_shell(image_obj.layers[curr_layer])
    # prep layers based on the chosen driver
    target = prep_layers(image_obj, curr_layer, options.driver)
    # set this layer's host path
    prereqs.host_path = target
    # get commands that created the layer
    # for docker images this is retrieved from the image history
    command_list = dcom.get_commands_from_metadata(
        image_obj.layers[curr_layer])
    if command_list:
        # for each command look up the snippet library
        for command in command_list:
            pkg_listing = command_lib.get_package_listing(command.name)
            if isinstance(pkg_listing, str):
                prereqs.binary = pkg_listing
                core.execute_base(
                    image_obj.layers[curr_layer], prereqs)
            else:
                prereqs.listing = pkg_listing
                core.execute_snippets(
                    image_obj.layers[curr_layer], command, prereqs)
    else:
        # fall back to executing what we know
        core.execute_base(image_obj.layers[curr_layer], prereqs)
    # if the driver is using some storage driver, unmount the rootfs
    if options.driver != 'default':
        rootfs.unmount_rootfs()


def analyze_subsequent_layers(image_obj, prereqs, master_list, options):
    """Assuming we have completed analyzing the first layer of the given image
    object, we now analyze the remaining layers.
    While we have layers:
        1. Check if the layer is empty. If it is, then we can't do anything and
        we should continue
        2. See if we can load the layer from cache. If we can't then do a
        fresh analysis
        package information and bundle it into the image object
        3. Update the master list"""
    # if the driver is 'default' then the first layer needs to be extracted
    mergepath = os.path.join(rootfs.get_working_dir(), constants.mergedir)
    if not os.listdir(mergepath):
        prep_layers(image_obj, 0, 'default')
    curr_layer = 1
    # get list of environment variables
    prereqs.envs = lock.get_env_vars(image_obj)
    while curr_layer < len(image_obj.layers):
        # If work_dir changes, update value accordingly
        # so we can later execute base.yml commands from the work_dir
        if image_obj.layers[curr_layer].get_layer_workdir():
            prereqs.layer_workdir = \
                image_obj.layers[curr_layer].get_layer_workdir()
        # make a notice for each layer
        origin_next_layer = 'Layer {}'.format(
            image_obj.layers[curr_layer].layer_index)
        # check if this is an empty layer
        if common.is_empty_layer(image_obj.layers[curr_layer]):
            # we continue to the next layer
            logger.warning(errors.empty_layer)
            image_obj.layers[curr_layer].origins.add_notice_to_origins(
                origin_next_layer, Notice(errors.empty_layer, 'warning'))
            curr_layer = curr_layer + 1
            continue
        if not common.load_from_cache(image_obj.layers[curr_layer],
                                      options.redo):
            fresh_analysis(image_obj, curr_layer, prereqs, options)
        # If the driver is 'default' apply the current layer anyway
        if options.driver == 'default':
            apply_layers(image_obj, curr_layer)
        # update the master list
        dcom.update_master_list(master_list, image_obj.layers[curr_layer])
        curr_layer = curr_layer + 1
