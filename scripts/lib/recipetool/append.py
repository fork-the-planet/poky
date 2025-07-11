# Recipe creation tool - append plugin
#
# Copyright (C) 2015 Intel Corporation
#
# SPDX-License-Identifier: GPL-2.0-only
#

import sys
import os
import argparse
import glob
import fnmatch
import re
import subprocess
import logging
import stat
import shutil
import scriptutils
import errno
from collections import defaultdict
import difflib

logger = logging.getLogger('recipetool')

tinfoil = None

def tinfoil_init(instance):
    global tinfoil
    tinfoil = instance


# FIXME guessing when we don't have pkgdata?
# FIXME mode to create patch rather than directly substitute

class InvalidTargetFileError(Exception):
    pass

def find_target_file(targetpath, d, pkglist=None):
    """Find the recipe installing the specified target path, optionally limited to a select list of packages"""
    import json

    pkgdata_dir = d.getVar('PKGDATA_DIR')

    # The mix between /etc and ${sysconfdir} here may look odd, but it is just
    # being consistent with usage elsewhere
    invalidtargets = {'${sysconfdir}/version': '${sysconfdir}/version is written out at image creation time',
                      '/etc/timestamp': '/etc/timestamp is written out at image creation time',
                      '/dev/*': '/dev is handled by udev (or equivalent) and the kernel (devtmpfs)',
                      '/etc/passwd': '/etc/passwd should be managed through the useradd and extrausers classes',
                      '/etc/group': '/etc/group should be managed through the useradd and extrausers classes',
                      '/etc/shadow': '/etc/shadow should be managed through the useradd and extrausers classes',
                      '/etc/gshadow': '/etc/gshadow should be managed through the useradd and extrausers classes',
                      '${sysconfdir}/hostname': '${sysconfdir}/hostname contents should be set by setting hostname:pn-base-files = "value" in configuration',}

    for pthspec, message in invalidtargets.items():
        if fnmatch.fnmatchcase(targetpath, d.expand(pthspec)):
            raise InvalidTargetFileError(d.expand(message))

    targetpath_re = re.compile(r'\s+(\$D)?%s(\s|$)' % targetpath)

    recipes = defaultdict(list)
    for root, dirs, files in os.walk(os.path.join(pkgdata_dir, 'runtime')):
        if pkglist:
            filelist = pkglist
        else:
            filelist = files
        for fn in filelist:
            pkgdatafile = os.path.join(root, fn)
            if pkglist and not os.path.exists(pkgdatafile):
                continue
            with open(pkgdatafile, 'r') as f:
                pn = ''
                # This does assume that PN comes before other values, but that's a fairly safe assumption
                for line in f:
                    if line.startswith('PN:'):
                        pn = line.split(': ', 1)[1].strip()
                    elif line.startswith('FILES_INFO'):
                        val = line.split(': ', 1)[1].strip()
                        dictval = json.loads(val)
                        for fullpth in dictval.keys():
                            if fnmatch.fnmatchcase(fullpth, targetpath):
                                recipes[targetpath].append(pn)
                    elif line.startswith('pkg_preinst:') or line.startswith('pkg_postinst:'):
                        scriptval = line.split(': ', 1)[1].strip().encode('utf-8').decode('unicode_escape')
                        if 'update-alternatives --install %s ' % targetpath in scriptval:
                            recipes[targetpath].append('?%s' % pn)
                        elif targetpath_re.search(scriptval):
                            recipes[targetpath].append('!%s' % pn)
    return recipes

def _parse_recipe(pn, tinfoil):
    try:
        rd = tinfoil.parse_recipe(pn)
    except bb.providers.NoProvider as e:
        logger.error(str(e))
        return None
    return rd

def determine_file_source(targetpath, rd):
    """Assuming we know a file came from a specific recipe, figure out exactly where it came from"""
    import oe.recipeutils

    # See if it's in do_install for the recipe
    unpackdir = rd.getVar('UNPACKDIR')
    src_uri = rd.getVar('SRC_URI')
    srcfile = ''
    modpatches = []
    elements = check_do_install(rd, targetpath)
    if elements:
        logger.debug('do_install line:\n%s' % ' '.join(elements))
        srcpath = get_source_path(elements)
        logger.debug('source path: %s' % srcpath)
        if not srcpath.startswith('/'):
            # Handle non-absolute path
            srcpath = os.path.abspath(os.path.join(rd.getVarFlag('do_install', 'dirs').split()[-1], srcpath))
        if srcpath.startswith(unpackdir):
            # OK, now we have the source file name, look for it in SRC_URI
            workdirfile = os.path.relpath(srcpath, unpackdir)
            # FIXME this is where we ought to have some code in the fetcher, because this is naive
            for item in src_uri.split():
                localpath = bb.fetch2.localpath(item, rd)
                # Source path specified in do_install might be a glob
                if fnmatch.fnmatch(os.path.basename(localpath), workdirfile):
                    srcfile = 'file://%s' % localpath
                elif '/' in workdirfile:
                    if item == 'file://%s' % workdirfile:
                        srcfile = 'file://%s' % localpath

        # Check patches
        srcpatches = []
        patchedfiles = oe.recipeutils.get_recipe_patched_files(rd)
        for patch, filelist in patchedfiles.items():
            for fileitem in filelist:
                if fileitem[0] == srcpath:
                    srcpatches.append((patch, fileitem[1]))
        if srcpatches:
            addpatch = None
            for patch in srcpatches:
                if patch[1] == 'A':
                    addpatch = patch[0]
                else:
                    modpatches.append(patch[0])
            if addpatch:
                srcfile = 'patch://%s' % addpatch

    return (srcfile, elements, modpatches)

def get_source_path(cmdelements):
    """Find the source path specified within a command"""
    command = cmdelements[0]
    if command in ['install', 'cp']:
        helptext = subprocess.check_output('LC_ALL=C %s --help' % command, shell=True).decode('utf-8')
        argopts = ''
        argopt_line_re = re.compile('^-([a-zA-Z0-9]), --[a-z-]+=')
        for line in helptext.splitlines():
            line = line.lstrip()
            res = argopt_line_re.search(line)
            if res:
                argopts += res.group(1)
        if not argopts:
            # Fallback
            if command == 'install':
                argopts = 'gmoSt'
            elif command == 'cp':
                argopts = 't'
            else:
                raise Exception('No fallback arguments for command %s' % command)

        skipnext = False
        for elem in cmdelements[1:-1]:
            if elem.startswith('-'):
                if len(elem) > 1 and elem[1] in argopts:
                    skipnext = True
                continue
            if skipnext:
                skipnext = False
                continue
            return elem
    else:
        raise Exception('get_source_path: no handling for command "%s"')

def get_func_deps(func, d):
    """Find the function dependencies of a shell function"""
    deps = bb.codeparser.ShellParser(func, logger).parse_shell(d.getVar(func))
    deps |= set((d.getVarFlag(func, "vardeps") or "").split())
    funcdeps = []
    for dep in deps:
        if d.getVarFlag(dep, 'func'):
            funcdeps.append(dep)
    return funcdeps

def check_do_install(rd, targetpath):
    """Look at do_install for a command that installs/copies the specified target path"""
    instpath = os.path.abspath(os.path.join(rd.getVar('D'), targetpath.lstrip('/')))
    do_install = rd.getVar('do_install')
    # Handle where do_install calls other functions (somewhat crudely, but good enough for this purpose)
    deps = get_func_deps('do_install', rd)
    for dep in deps:
        do_install = do_install.replace(dep, rd.getVar(dep))

    # Look backwards through do_install as we want to catch where a later line (perhaps
    # from a bbappend) is writing over the top
    for line in reversed(do_install.splitlines()):
        line = line.strip()
        if (line.startswith('install ') and ' -m' in line) or line.startswith('cp '):
            elements = line.split()
            destpath = os.path.abspath(elements[-1])
            if destpath == instpath:
                return elements
            elif destpath.rstrip('/') == os.path.dirname(instpath):
                # FIXME this doesn't take recursive copy into account; unsure if it's practical to do so
                srcpath = get_source_path(elements)
                if fnmatch.fnmatchcase(os.path.basename(instpath), os.path.basename(srcpath)):
                    return elements
    return None


def appendfile(args):
    import oe.recipeutils

    stdout = ''
    try:
        (stdout, _) = bb.process.run('LANG=C file -b %s' % args.newfile, shell=True)
        if 'cannot open' in stdout:
            raise bb.process.ExecutionError(stdout)
    except bb.process.ExecutionError as err:
        logger.debug('file command returned error: %s' % err)
        stdout = ''
    if stdout:
        logger.debug('file command output: %s' % stdout.rstrip())
        if ('executable' in stdout and not 'shell script' in stdout) or 'shared object' in stdout:
            logger.warning('This file looks like it is a binary or otherwise the output of compilation. If it is, you should consider building it properly instead of substituting a binary file directly.')

    if args.recipe:
        recipes = {args.targetpath: [args.recipe],}
    else:
        try:
            recipes = find_target_file(args.targetpath, tinfoil.config_data)
        except InvalidTargetFileError as e:
            logger.error('%s cannot be handled by this tool: %s' % (args.targetpath, e))
            return 1
        if not recipes:
            logger.error('Unable to find any package producing path %s - this may be because the recipe packaging it has not been built yet' % args.targetpath)
            return 1

    alternative_pns = []
    postinst_pns = []

    selectpn = None
    for targetpath, pnlist in recipes.items():
        for pn in pnlist:
            if pn.startswith('?'):
                alternative_pns.append(pn[1:])
            elif pn.startswith('!'):
                postinst_pns.append(pn[1:])
            elif selectpn:
                # hit here with multilibs
                continue
            else:
                selectpn = pn

    if not selectpn and len(alternative_pns) == 1:
        selectpn = alternative_pns[0]
        logger.error('File %s is an alternative possibly provided by recipe %s but seemingly no other, selecting it by default - you should double check other recipes' % (args.targetpath, selectpn))

    if selectpn:
        logger.debug('Selecting recipe %s for file %s' % (selectpn, args.targetpath))
        if postinst_pns:
            logger.warning('%s be modified by postinstall scripts for the following recipes:\n  %s\nThis may or may not be an issue depending on what modifications these postinstall scripts make.' % (args.targetpath, '\n  '.join(postinst_pns)))
        rd = _parse_recipe(selectpn, tinfoil)
        if not rd:
            # Error message already shown
            return 1
        sourcefile, instelements, modpatches = determine_file_source(args.targetpath, rd)
        sourcepath = None
        if sourcefile:
            sourcetype, sourcepath = sourcefile.split('://', 1)
            logger.debug('Original source file is %s (%s)' % (sourcepath, sourcetype))
            if sourcetype == 'patch':
                logger.warning('File %s is added by the patch %s - you may need to remove or replace this patch in order to replace the file.' % (args.targetpath, sourcepath))
                sourcepath = None
        else:
            logger.debug('Unable to determine source file, proceeding anyway')
        if modpatches:
            logger.warning('File %s is modified by the following patches:\n  %s' % (args.targetpath, '\n  '.join(modpatches)))

        if instelements and sourcepath:
            install = None
        else:
            # Auto-determine permissions
            # Check destination
            binpaths = '${bindir}:${sbindir}:${base_bindir}:${base_sbindir}:${libexecdir}:${sysconfdir}/init.d'
            perms = '0644'
            if os.path.abspath(os.path.dirname(args.targetpath)) in rd.expand(binpaths).split(':'):
                # File is going into a directory normally reserved for executables, so it should be executable
                perms = '0755'
            else:
                # Check source
                st = os.stat(args.newfile)
                if st.st_mode & stat.S_IXUSR:
                    perms = '0755'
            install = {args.newfile: (args.targetpath, perms)}
        if sourcepath:
            sourcepath = os.path.basename(sourcepath)
        oe.recipeutils.bbappend_recipe(rd, args.destlayer, {args.newfile: {'newname' : sourcepath}}, install, wildcardver=args.wildcard_version, machine=args.machine)
        tinfoil.modified_files()
        return 0
    else:
        if alternative_pns:
            logger.error('File %s is an alternative possibly provided by the following recipes:\n  %s\nPlease select recipe with -r/--recipe' % (targetpath, '\n  '.join(alternative_pns)))
        elif postinst_pns:
            logger.error('File %s may be written out in a pre/postinstall script of the following recipes:\n  %s\nPlease select recipe with -r/--recipe' % (targetpath, '\n  '.join(postinst_pns)))
        return 3


def appendsrc(args, files, rd, extralines=None):
    import oe.recipeutils

    srcdir = rd.getVar('S')
    unpackdir = rd.getVar('UNPACKDIR')

    import bb.fetch
    simplified = {}
    src_uri = rd.getVar('SRC_URI').split()
    for uri in src_uri:
        if uri.endswith(';'):
            uri = uri[:-1]
        simple_uri = bb.fetch.URI(uri)
        simple_uri.params = {}
        simplified[str(simple_uri)] = uri

    copyfiles = {}
    extralines = extralines or []
    params = []
    for newfile, srcfile in files.items():
        src_destdir = os.path.dirname(srcfile)
        if not args.use_workdir:
            if rd.getVar('S') == rd.getVar('STAGING_KERNEL_DIR'):
                srcdir = os.path.join(unpackdir, rd.getVar('BB_GIT_DEFAULT_DESTSUFFIX'))
                if not bb.data.inherits_class('kernel-yocto', rd):
                    logger.warning('S == STAGING_KERNEL_DIR and non-kernel-yocto, unable to determine path to srcdir, defaulting to ${UNPACKDIR}/${BB_GIT_DEFAULT_DESTSUFFIX}')
            src_destdir = os.path.join(os.path.relpath(srcdir, unpackdir), src_destdir)
        src_destdir = os.path.normpath(src_destdir)

        if src_destdir and src_destdir != '.':
            params.append({'subdir': src_destdir})
        else:
            params.append({})

        copyfiles[newfile] = {'newname' : os.path.basename(srcfile)}

    dry_run_output = None
    dry_run_outdir = None
    if args.dry_run:
        import tempfile
        dry_run_output = tempfile.TemporaryDirectory(prefix='devtool')
        dry_run_outdir = dry_run_output.name

    appendfile, _ = oe.recipeutils.bbappend_recipe(rd, args.destlayer, copyfiles, None, wildcardver=args.wildcard_version, machine=args.machine, extralines=extralines, params=params,
                                                   redirect_output=dry_run_outdir, update_original_recipe=args.update_recipe)
    if not appendfile:
        return
    if args.dry_run:
        output = ''
        appendfilename = os.path.basename(appendfile)
        newappendfile = appendfile
        if appendfile and os.path.exists(appendfile):
            with open(appendfile, 'r') as f:
                oldlines = f.readlines()
        else:
            appendfile = '/dev/null'
            oldlines = []

        with open(os.path.join(dry_run_outdir, appendfilename), 'r') as f:
            newlines = f.readlines()
        diff = difflib.unified_diff(oldlines, newlines, appendfile, newappendfile)
        difflines = list(diff)
        if difflines:
            output += ''.join(difflines)
        if output:
            logger.info('Diff of changed files:\n%s' % output)
        else:
            logger.info('No changed files')
    tinfoil.modified_files()

def appendsrcfiles(parser, args):
    recipedata = _parse_recipe(args.recipe, tinfoil)
    if not recipedata:
        parser.error('RECIPE must be a valid recipe name')

    files = dict((f, os.path.join(args.destdir, os.path.basename(f)))
                 for f in args.files)
    return appendsrc(args, files, recipedata)


def appendsrcfile(parser, args):
    recipedata = _parse_recipe(args.recipe, tinfoil)
    if not recipedata:
        parser.error('RECIPE must be a valid recipe name')

    if not args.destfile:
        args.destfile = os.path.basename(args.file)
    elif args.destfile.endswith('/'):
        args.destfile = os.path.join(args.destfile, os.path.basename(args.file))

    return appendsrc(args, {args.file: args.destfile}, recipedata)


def layer(layerpath):
    if not os.path.exists(os.path.join(layerpath, 'conf', 'layer.conf')):
        raise argparse.ArgumentTypeError('{0!r} must be a path to a valid layer'.format(layerpath))
    return layerpath


def existing_path(filepath):
    if not os.path.exists(filepath):
        raise argparse.ArgumentTypeError('{0!r} must be an existing path'.format(filepath))
    return filepath


def existing_file(filepath):
    filepath = existing_path(filepath)
    if os.path.isdir(filepath):
        raise argparse.ArgumentTypeError('{0!r} must be a file, not a directory'.format(filepath))
    return filepath


def destination_path(destpath):
    if os.path.isabs(destpath):
        raise argparse.ArgumentTypeError('{0!r} must be a relative path, not absolute'.format(destpath))
    return destpath


def target_path(targetpath):
    if not os.path.isabs(targetpath):
        raise argparse.ArgumentTypeError('{0!r} must be an absolute path, not relative'.format(targetpath))
    return targetpath


def register_commands(subparsers):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('-m', '--machine', help='Make bbappend changes specific to a machine only', metavar='MACHINE')
    common.add_argument('-w', '--wildcard-version', help='Use wildcard to make the bbappend apply to any recipe version', action='store_true')
    common.add_argument('destlayer', metavar='DESTLAYER', help='Base directory of the destination layer to write the bbappend to', type=layer)

    parser_appendfile = subparsers.add_parser('appendfile',
                                              parents=[common],
                                              help='Create/update a bbappend to replace a target file',
                                              description='Creates a bbappend (or updates an existing one) to replace the specified file that appears in the target system, determining the recipe that packages the file and the required path and name for the bbappend automatically. Note that the ability to determine the recipe packaging a particular file depends upon the recipe\'s do_packagedata task having already run prior to running this command (which it will have when the recipe has been built successfully, which in turn will have happened if one or more of the recipe\'s packages is included in an image that has been built successfully).')
    parser_appendfile.add_argument('targetpath', help='Path to the file to be replaced (as it would appear within the target image, e.g. /etc/motd)', type=target_path)
    parser_appendfile.add_argument('newfile', help='Custom file to replace the target file with', type=existing_file)
    parser_appendfile.add_argument('-r', '--recipe', help='Override recipe to apply to (default is to find which recipe already packages the file)')
    parser_appendfile.set_defaults(func=appendfile, parserecipes=True)

    common_src = argparse.ArgumentParser(add_help=False, parents=[common])
    common_src.add_argument('-W', '--workdir', help='Unpack file into WORKDIR rather than S', dest='use_workdir', action='store_true')
    common_src.add_argument('recipe', metavar='RECIPE', help='Override recipe to apply to')

    parser = subparsers.add_parser('appendsrcfiles',
                                   parents=[common_src],
                                   help='Create/update a bbappend to add or replace source files',
                                   description='Creates a bbappend (or updates an existing one) to add or replace the specified file in the recipe sources, either those in WORKDIR or those in the source tree. This command lets you specify multiple files with a destination directory, so cannot specify the destination filename. See the `appendsrcfile` command for the other behavior.')
    parser.add_argument('-D', '--destdir', help='Destination directory (relative to S or WORKDIR, defaults to ".")', default='', type=destination_path)
    parser.add_argument('-u', '--update-recipe', help='Update recipe instead of creating (or updating) a bbapend file. DESTLAYER must contains the recipe to update', action='store_true')
    parser.add_argument('-n', '--dry-run', help='Dry run mode', action='store_true')
    parser.add_argument('files', nargs='+', metavar='FILE', help='File(s) to be added to the recipe sources (WORKDIR or S)', type=existing_path)
    parser.set_defaults(func=lambda a: appendsrcfiles(parser, a), parserecipes=True)

    parser = subparsers.add_parser('appendsrcfile',
                                   parents=[common_src],
                                   help='Create/update a bbappend to add or replace a source file',
                                   description='Creates a bbappend (or updates an existing one) to add or replace the specified files in the recipe sources, either those in WORKDIR or those in the source tree. This command lets you specify the destination filename, not just destination directory, but only works for one file. See the `appendsrcfiles` command for the other behavior.')
    parser.add_argument('-u', '--update-recipe', help='Update recipe instead of creating (or updating) a bbapend file. DESTLAYER must contains the recipe to update', action='store_true')
    parser.add_argument('-n', '--dry-run', help='Dry run mode', action='store_true')
    parser.add_argument('file', metavar='FILE', help='File to be added to the recipe sources (WORKDIR or S)', type=existing_path)
    parser.add_argument('destfile', metavar='DESTFILE', nargs='?', help='Destination path (relative to S or WORKDIR, optional)', type=destination_path)
    parser.set_defaults(func=lambda a: appendsrcfile(parser, a), parserecipes=True)
