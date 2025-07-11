# Recipe creation tool - create command plugin
#
# Copyright (C) 2014-2017 Intel Corporation
#
# SPDX-License-Identifier: GPL-2.0-only
#

import sys
import os
import argparse
import glob
import fnmatch
import re
import json
import logging
import scriptutils
from urllib.parse import urlparse, urldefrag, urlsplit
import hashlib
import bb.fetch2
logger = logging.getLogger('recipetool')
from oe.license import tidy_licenses
from oe.license_finder import find_licenses

tinfoil = None
plugins = None

def log_error_cond(message, debugonly):
    if debugonly:
        logger.debug(message)
    else:
        logger.error(message)

def log_info_cond(message, debugonly):
    if debugonly:
        logger.debug(message)
    else:
        logger.info(message)

def plugin_init(pluginlist):
    # Take a reference to the list so we can use it later
    global plugins
    plugins = pluginlist

def tinfoil_init(instance):
    global tinfoil
    tinfoil = instance

class RecipeHandler(object):
    recipelibmap = {}
    recipeheadermap = {}
    recipecmakefilemap = {}
    recipebinmap = {}

    def __init__(self):
        self._devtool = False

    @staticmethod
    def load_libmap(d):
        '''Load library->recipe mapping'''
        import oe.package

        if RecipeHandler.recipelibmap:
            return
        # First build up library->package mapping
        d2 = bb.data.createCopy(d)
        d2.setVar("WORKDIR_PKGDATA", "${PKGDATA_DIR}")
        shlib_providers = oe.package.read_shlib_providers(d2)
        libdir = d.getVar('libdir')
        base_libdir = d.getVar('base_libdir')
        libpaths = list(set([base_libdir, libdir]))
        libname_re = re.compile(r'^lib(.+)\.so.*$')
        pkglibmap = {}
        for lib, item in shlib_providers.items():
            for path, pkg in item.items():
                if path in libpaths:
                    res = libname_re.match(lib)
                    if res:
                        libname = res.group(1)
                        if not libname in pkglibmap:
                            pkglibmap[libname] = pkg[0]
                    else:
                        logger.debug('unable to extract library name from %s' % lib)

        # Now turn it into a library->recipe mapping
        pkgdata_dir = d.getVar('PKGDATA_DIR')
        for libname, pkg in pkglibmap.items():
            try:
                with open(os.path.join(pkgdata_dir, 'runtime', pkg)) as f:
                    for line in f:
                        if line.startswith('PN:'):
                            RecipeHandler.recipelibmap[libname] = line.split(':', 1)[-1].strip()
                            break
            except IOError as ioe:
                if ioe.errno == 2:
                    logger.warning('unable to find a pkgdata file for package %s' % pkg)
                else:
                    raise

        # Some overrides - these should be mapped to the virtual
        RecipeHandler.recipelibmap['GL'] = 'virtual/libgl'
        RecipeHandler.recipelibmap['EGL'] = 'virtual/egl'
        RecipeHandler.recipelibmap['GLESv2'] = 'virtual/libgles2'

    @staticmethod
    def load_devel_filemap(d):
        '''Build up development file->recipe mapping'''
        if RecipeHandler.recipeheadermap:
            return
        pkgdata_dir = d.getVar('PKGDATA_DIR')
        includedir = d.getVar('includedir')
        cmakedir = os.path.join(d.getVar('libdir'), 'cmake')
        for pkg in glob.glob(os.path.join(pkgdata_dir, 'runtime', '*-dev')):
            with open(os.path.join(pkgdata_dir, 'runtime', pkg)) as f:
                pn = None
                headers = []
                cmakefiles = []
                for line in f:
                    if line.startswith('PN:'):
                        pn = line.split(':', 1)[-1].strip()
                    elif line.startswith('FILES_INFO:%s:' % pkg):
                        val = line.split(': ', 1)[1].strip()
                        dictval = json.loads(val)
                        for fullpth in sorted(dictval):
                            if fullpth.startswith(includedir) and fullpth.endswith('.h'):
                                headers.append(os.path.relpath(fullpth, includedir))
                            elif fullpth.startswith(cmakedir) and fullpth.endswith('.cmake'):
                                cmakefiles.append(os.path.relpath(fullpth, cmakedir))
                if pn and headers:
                    for header in headers:
                        RecipeHandler.recipeheadermap[header] = pn
                if pn and cmakefiles:
                    for fn in cmakefiles:
                        RecipeHandler.recipecmakefilemap[fn] = pn

    @staticmethod
    def load_binmap(d):
        '''Build up native binary->recipe mapping'''
        if RecipeHandler.recipebinmap:
            return
        sstate_manifests = d.getVar('SSTATE_MANIFESTS')
        staging_bindir_native = d.getVar('STAGING_BINDIR_NATIVE')
        build_arch = d.getVar('BUILD_ARCH')
        fileprefix = 'manifest-%s-' % build_arch
        for fn in glob.glob(os.path.join(sstate_manifests, '%s*-native.populate_sysroot' % fileprefix)):
            with open(fn, 'r') as f:
                pn = os.path.basename(fn).rsplit('.', 1)[0][len(fileprefix):]
                for line in f:
                    if line.startswith(staging_bindir_native):
                        prog = os.path.basename(line.rstrip())
                        RecipeHandler.recipebinmap[prog] = pn

    @staticmethod
    def checkfiles(path, speclist, recursive=False, excludedirs=None):
        results = []
        if recursive:
            for root, dirs, files in os.walk(path, topdown=True):
                if excludedirs:
                    dirs[:] = [d for d in dirs if d not in excludedirs]
                for fn in files:
                    for spec in speclist:
                        if fnmatch.fnmatch(fn, spec):
                            results.append(os.path.join(root, fn))
        else:
            for spec in speclist:
                results.extend(glob.glob(os.path.join(path, spec)))
        return results

    @staticmethod
    def handle_depends(libdeps, pcdeps, deps, outlines, values, d):
        if pcdeps:
            recipemap = read_pkgconfig_provides(d)
        if libdeps:
            RecipeHandler.load_libmap(d)

        ignorelibs = ['socket']
        ignoredeps = ['gcc-runtime', 'glibc', 'uclibc', 'musl', 'tar-native', 'binutils-native', 'coreutils-native']

        unmappedpc = []
        pcdeps = list(set(pcdeps))
        for pcdep in pcdeps:
            if isinstance(pcdep, str):
                recipe = recipemap.get(pcdep, None)
                if recipe:
                    deps.append(recipe)
                else:
                    if not pcdep.startswith('$'):
                        unmappedpc.append(pcdep)
            else:
                for item in pcdep:
                    recipe = recipemap.get(pcdep, None)
                    if recipe:
                        deps.append(recipe)
                        break
                else:
                    unmappedpc.append('(%s)' % ' or '.join(pcdep))

        unmappedlibs = []
        for libdep in libdeps:
            if isinstance(libdep, tuple):
                lib, header = libdep
            else:
                lib = libdep
                header = None

            if lib in ignorelibs:
                logger.debug('Ignoring library dependency %s' % lib)
                continue

            recipe = RecipeHandler.recipelibmap.get(lib, None)
            if recipe:
                deps.append(recipe)
            elif recipe is None:
                if header:
                    RecipeHandler.load_devel_filemap(d)
                    recipe = RecipeHandler.recipeheadermap.get(header, None)
                    if recipe:
                        deps.append(recipe)
                    elif recipe is None:
                        unmappedlibs.append(lib)
                else:
                    unmappedlibs.append(lib)

        deps = set(deps).difference(set(ignoredeps))

        if unmappedpc:
            outlines.append('# NOTE: unable to map the following pkg-config dependencies: %s' % ' '.join(unmappedpc))
            outlines.append('#       (this is based on recipes that have previously been built and packaged)')

        if unmappedlibs:
            outlines.append('# NOTE: the following library dependencies are unknown, ignoring: %s' % ' '.join(list(set(unmappedlibs))))
            outlines.append('#       (this is based on recipes that have previously been built and packaged)')

        if deps:
            values['DEPENDS'] = ' '.join(deps)

    @staticmethod
    def genfunction(outlines, funcname, content, python=False, forcespace=False):
        if python:
            prefix = 'python '
        else:
            prefix = ''
        outlines.append('%s%s () {' % (prefix, funcname))
        if python or forcespace:
            indent = '    '
        else:
            indent = '\t'
        addnoop = not python
        for line in content:
            outlines.append('%s%s' % (indent, line))
            if addnoop:
                strippedline = line.lstrip()
                if strippedline and not strippedline.startswith('#'):
                    addnoop = False
        if addnoop:
            # Without this there'll be a syntax error
            outlines.append('%s:' % indent)
        outlines.append('}')
        outlines.append('')

    def process(self, srctree, classes, lines_before, lines_after, handled, extravalues):
        return False


def validate_pv(pv):
    if not pv or '_version' in pv.lower() or pv[0] not in '0123456789':
        return False
    return True

def determine_from_filename(srcfile):
    """Determine name and version from a filename"""
    if is_package(srcfile):
        # Force getting the value from the package metadata
        return None, None

    if '.tar.' in srcfile:
        namepart = srcfile.split('.tar.')[0]
    else:
        namepart = os.path.splitext(srcfile)[0]
    namepart = namepart.lower().replace('_', '-')
    if namepart.endswith('.src'):
        namepart = namepart[:-4]
    if namepart.endswith('.orig'):
        namepart = namepart[:-5]
    splitval = namepart.split('-')
    logger.debug('determine_from_filename: split name %s into: %s' % (srcfile, splitval))

    ver_re = re.compile('^v?[0-9]')

    pv = None
    pn = None
    if len(splitval) == 1:
        # Try to split the version out if there is no separator (or a .)
        res = re.match('^([^0-9]+)([0-9.]+.*)$', namepart)
        if res:
            if len(res.group(1)) > 1 and len(res.group(2)) > 1:
                pn = res.group(1).rstrip('.')
                pv = res.group(2)
        else:
            pn = namepart
    else:
        if splitval[-1] in ['source', 'src']:
            splitval.pop()
        if len(splitval) > 2 and re.match('^(alpha|beta|stable|release|rc[0-9]|pre[0-9]|p[0-9]|[0-9]{8})', splitval[-1]) and ver_re.match(splitval[-2]):
            pv = '-'.join(splitval[-2:])
            if pv.endswith('-release'):
                pv = pv[:-8]
            splitval = splitval[:-2]
        elif ver_re.match(splitval[-1]):
            pv = splitval.pop()
        pn = '-'.join(splitval)
        if pv and pv.startswith('v'):
            pv = pv[1:]
    logger.debug('determine_from_filename: name = "%s" version = "%s"' % (pn, pv))
    return (pn, pv)

def determine_from_url(srcuri):
    """Determine name and version from a URL"""
    pn = None
    pv = None
    parseres = urlparse(srcuri.lower().split(';', 1)[0])
    if parseres.path:
        if 'github.com' in parseres.netloc:
            res = re.search(r'.*/(.*?)/archive/(.*)-final\.(tar|zip)', parseres.path)
            if res:
                pn = res.group(1).strip().replace('_', '-')
                pv = res.group(2).strip().replace('_', '.')
            else:
                res = re.search(r'.*/(.*?)/archive/v?(.*)\.(tar|zip)', parseres.path)
                if res:
                    pn = res.group(1).strip().replace('_', '-')
                    pv = res.group(2).strip().replace('_', '.')
        elif 'bitbucket.org' in parseres.netloc:
            res = re.search(r'.*/(.*?)/get/[a-zA-Z_-]*([0-9][0-9a-zA-Z_.]*)\.(tar|zip)', parseres.path)
            if res:
                pn = res.group(1).strip().replace('_', '-')
                pv = res.group(2).strip().replace('_', '.')

        if not pn and not pv:
            if parseres.scheme not in ['git', 'gitsm', 'svn', 'hg']:
                srcfile = os.path.basename(parseres.path.rstrip('/'))
                pn, pv = determine_from_filename(srcfile)
            elif parseres.scheme in ['git', 'gitsm']:
                pn = os.path.basename(parseres.path.rstrip('/')).lower().replace('_', '-')
                if pn.endswith('.git'):
                    pn = pn[:-4]

    logger.debug('Determined from source URL: name = "%s", version = "%s"' % (pn, pv))
    return (pn, pv)

def supports_srcrev(uri):
    localdata = bb.data.createCopy(tinfoil.config_data)
    # This is a bit sad, but if you don't have this set there can be some
    # odd interactions with the urldata cache which lead to errors
    localdata.setVar('SRCREV', '${AUTOREV}')
    try:
        fetcher = bb.fetch2.Fetch([uri], localdata)
        urldata = fetcher.ud
        for u in urldata:
            if urldata[u].method.supports_srcrev():
                return True
    except bb.fetch2.FetchError as e:
        logger.debug('FetchError in supports_srcrev: %s' % str(e))
        # Fall back to basic check
        if uri.startswith(('git://', 'gitsm://')):
            return True
    return False

def reformat_git_uri(uri):
    '''Convert any http[s]://....git URI into git://...;protocol=http[s]'''
    checkuri = uri.split(';', 1)[0]
    if checkuri.endswith('.git') or '/git/' in checkuri or re.match('https?://git(hub|lab).com/[^/]+/[^/]+/?$', checkuri):
        # Appends scheme if the scheme is missing
        if not '://' in uri:
            uri = 'git://' + uri
        scheme, host, path, user, pswd, parms = bb.fetch2.decodeurl(uri)
        # Detection mechanism, this is required due to certain URL are formatter with ":" rather than "/"
        # which causes decodeurl to fail getting the right host and path
        if len(host.split(':')) > 1:
            splitslash = host.split(':')
            # Port number should not be split from host
            if not re.match('^[0-9]+$', splitslash[1]):
                host = splitslash[0]
                path = '/' + splitslash[1] + path
        #Algorithm:
        # if user is defined, append protocol=ssh or if a protocol is defined, then honor the user-defined protocol
        # if no user & password is defined, check for scheme type and append the protocol with the scheme type
        # finally if protocols or if the url is well-formed, do nothing and rejoin everything back to normal
        # Need to repackage the arguments for encodeurl, the format is: (scheme, host, path, user, password, OrderedDict([('key', 'value')]))
        if user:
            if not 'protocol' in parms:
                parms.update({('protocol', 'ssh')})
        elif (scheme == "http" or scheme == 'https' or scheme == 'ssh') and not ('protocol' in parms):
            parms.update({('protocol', scheme)})
        # Always append 'git://'
        fUrl = bb.fetch2.encodeurl(('git', host, path, user, pswd, parms))
        return fUrl
    else:
        return uri

def is_package(url):
    '''Check if a URL points to a package'''
    checkurl = url.split(';', 1)[0]
    if checkurl.endswith(('.deb', '.ipk', '.rpm', '.srpm')):
        return True
    return False

def create_recipe(args):
    import bb.process
    import tempfile
    import shutil
    import oe.recipeutils

    pkgarch = ""
    if args.machine:
        pkgarch = "${MACHINE_ARCH}"

    extravalues = {}
    checksums = {}
    tempsrc = ''
    source = args.source
    srcsubdir = ''
    srcrev = '${AUTOREV}'
    srcbranch = ''
    scheme = ''
    storeTagName = ''
    pv_srcpv = False

    handled = []
    classes = []

    # Find all plugins that want to register handlers
    logger.debug('Loading recipe handlers')
    raw_handlers = []
    for plugin in plugins:
        if hasattr(plugin, 'register_recipe_handlers'):
            plugin.register_recipe_handlers(raw_handlers)
    # Sort handlers by priority
    handlers = []
    for i, handler in enumerate(raw_handlers):
        if isinstance(handler, tuple):
            handlers.append((handler[0], handler[1], i))
        else:
            handlers.append((handler, 0, i))
    handlers.sort(key=lambda item: (item[1], -item[2]), reverse=True)
    for handler, priority, _ in handlers:
        logger.debug('Handler: %s (priority %d)' % (handler.__class__.__name__, priority))
        setattr(handler, '_devtool', args.devtool)
    handlers = [item[0] for item in handlers]

    fetchuri = None
    for handler in handlers:
        if hasattr(handler, 'process_url'):
            ret = handler.process_url(args, classes, handled, extravalues)
            if 'url' in handled and ret:
                fetchuri = ret
                break

    if os.path.isfile(source):
        source = 'file://%s' % os.path.abspath(source)

    if scriptutils.is_src_url(source):
        # Warn about github archive URLs
        if re.match(r'https?://github.com/[^/]+/[^/]+/archive/.+(\.tar\..*|\.zip)$', source):
            logger.warning('github archive files are not guaranteed to be stable and may be re-generated over time. If the latter occurs, the checksums will likely change and the recipe will fail at do_fetch. It is recommended that you point to an actual commit or tag in the repository instead (using the repository URL in conjunction with the -S/--srcrev option).')
        # Fetch a URL
        if not fetchuri:
            fetchuri = reformat_git_uri(urldefrag(source)[0])
        if args.binary:
            # Assume the archive contains the directory structure verbatim
            # so we need to extract to a subdirectory
            fetchuri += ';subdir=${BPN}'
        srcuri = fetchuri
        rev_re = re.compile(';rev=([^;]+)')
        res = rev_re.search(srcuri)
        if res:
            if args.srcrev:
                logger.error('rev= parameter and -S/--srcrev option cannot both be specified - use one or the other')
                sys.exit(1)
            if args.autorev:
                logger.error('rev= parameter and -a/--autorev option cannot both be specified - use one or the other')
                sys.exit(1)
            srcrev = res.group(1)
            srcuri = rev_re.sub('', srcuri)
        elif args.srcrev:
            srcrev = args.srcrev

        # Check whether users provides any branch info in fetchuri.
        # If true, we will skip all branch checking process to honor all user's input.
        scheme, network, path, user, passwd, params = bb.fetch2.decodeurl(fetchuri)
        srcbranch = params.get('branch')
        if args.srcbranch:
            if srcbranch:
                logger.error('branch= parameter and -B/--srcbranch option cannot both be specified - use one or the other')
                sys.exit(1)
            srcbranch = args.srcbranch
            params['branch'] = srcbranch
        nobranch = params.get('nobranch')
        if nobranch and srcbranch:
            logger.error('nobranch= cannot be used if you specify a branch')
            sys.exit(1)
        tag = params.get('tag')
        if not srcbranch and not nobranch and srcrev != '${AUTOREV}':
            # Append nobranch=1 in the following conditions:
            # 1. User did not set 'branch=' in srcuri, and
            # 2. User did not set 'nobranch=1' in srcuri, and
            # 3. Source revision is not '${AUTOREV}'
            params['nobranch'] = '1'
        if tag:
            # Keep a copy of tag and append nobranch=1 then remove tag from URL.
            # Bitbake fetcher unable to fetch when {AUTOREV} and tag is set at the same time.
            storeTagName = params['tag']
            params['nobranch'] = '1'
            del params['tag']
        # Assume 'master' branch if not set
        if scheme in ['git', 'gitsm'] and 'branch' not in params and 'nobranch' not in params:
            params['branch'] = 'master'
        fetchuri = bb.fetch2.encodeurl((scheme, network, path, user, passwd, params))

        tmpparent = tinfoil.config_data.getVar('BASE_WORKDIR')
        bb.utils.mkdirhier(tmpparent)
        tempsrc = tempfile.mkdtemp(prefix='recipetool-', dir=tmpparent)
        srctree = os.path.join(tempsrc, 'source')

        try:
            checksums, ftmpdir = scriptutils.fetch_url(tinfoil, fetchuri, srcrev, srctree, logger, preserve_tmp=args.keep_temp)
        except scriptutils.FetchUrlFailure as e:
            logger.error(str(e))
            sys.exit(1)

        if ftmpdir and args.keep_temp:
            logger.info('Fetch temp directory is %s' % ftmpdir)

        dirlist = os.listdir(srctree)
        logger.debug('Directory listing (excluding filtered out):\n  %s' % '\n  '.join(dirlist))
        if len(dirlist) == 1:
            singleitem = os.path.join(srctree, dirlist[0])
            if os.path.isdir(singleitem):
                # We unpacked a single directory, so we should use that
                srcsubdir = dirlist[0]
                srctree = os.path.join(srctree, srcsubdir)
            else:
                check_single_file(dirlist[0], fetchuri)
        elif len(dirlist) == 0:
            if '/' in fetchuri:
                fn = os.path.join(tinfoil.config_data.getVar('DL_DIR'), fetchuri.split('/')[-1])
                if os.path.isfile(fn):
                    check_single_file(fn, fetchuri)
            # If we've got to here then there's no source so we might as well give up
            logger.error('URL %s resulted in an empty source tree' % fetchuri)
            sys.exit(1)

        # We need this checking mechanism to improve the recipe created by recipetool and devtool
        # is able to parse and build by bitbake.
        # If there is no input for branch name, then check for branch name with SRCREV provided.
        if not srcbranch and not nobranch and srcrev and (srcrev != '${AUTOREV}') and scheme in ['git', 'gitsm']:
            try:
                cmd = 'git branch -r --contains'
                check_branch, check_branch_err = bb.process.run('%s %s' % (cmd, srcrev), cwd=srctree)
            except bb.process.ExecutionError as err:
                logger.error(str(err))
                sys.exit(1)
            get_branch = [x.strip() for x in check_branch.splitlines()]
            # Remove HEAD reference point and drop remote prefix
            get_branch = [x.split('/', 1)[1] for x in get_branch if not x.startswith('origin/HEAD')]
            if 'master' in get_branch:
                # Even with the case where get_branch has multiple objects, if 'master' is one
                # of them, we should default take from 'master'
                srcbranch = 'master'
            elif len(get_branch) == 1:
                # If 'master' isn't in get_branch and get_branch contains only ONE object, then store result into 'srcbranch'
                srcbranch = get_branch[0]
            else:
                # If get_branch contains more than one objects, then display error and exit.
                mbrch = '\n  ' + '\n  '.join(get_branch)
                logger.error('Revision %s was found on multiple branches: %s\nPlease provide the correct branch with -B/--srcbranch' % (srcrev, mbrch))
                sys.exit(1)

        # Since we might have a value in srcbranch, we need to
        # recontruct the srcuri to include 'branch' in params.
        scheme, network, path, user, passwd, params = bb.fetch2.decodeurl(srcuri)
        if scheme in ['git', 'gitsm']:
            params['branch'] = srcbranch or 'master'

        if storeTagName and scheme in ['git', 'gitsm']:
            # Check srcrev using tag and check validity of the tag
            cmd = ('git rev-parse --verify %s' % (storeTagName))
            try:
                check_tag, check_tag_err = bb.process.run('%s' % cmd, cwd=srctree)
                srcrev = check_tag.split()[0]
            except bb.process.ExecutionError as err:
                logger.error(str(err))
                logger.error("Possibly wrong tag name is provided")
                sys.exit(1)
            # Drop tag from srcuri as it will have conflicts with SRCREV during recipe parse.
            del params['tag']
        srcuri = bb.fetch2.encodeurl((scheme, network, path, user, passwd, params))

        if os.path.exists(os.path.join(srctree, '.gitmodules')) and srcuri.startswith('git://'):
            srcuri = 'gitsm://' + srcuri[6:]
            logger.info('Fetching submodules...')
            bb.process.run('git submodule update --init --recursive', cwd=srctree)

        if is_package(fetchuri):
            localdata = bb.data.createCopy(tinfoil.config_data)
            pkgfile = bb.fetch2.localpath(fetchuri, localdata)
            if pkgfile:
                tmpfdir = tempfile.mkdtemp(prefix='recipetool-')
                try:
                    if pkgfile.endswith(('.deb', '.ipk')):
                        stdout, _ = bb.process.run('ar x %s' % pkgfile, cwd=tmpfdir)
                        stdout, _ = bb.process.run('tar xf control.tar.gz', cwd=tmpfdir)
                        values = convert_debian(tmpfdir)
                        extravalues.update(values)
                    elif pkgfile.endswith(('.rpm', '.srpm')):
                        stdout, _ = bb.process.run('rpm -qp --xml %s > pkginfo.xml' % pkgfile, cwd=tmpfdir)
                        values = convert_rpm_xml(os.path.join(tmpfdir, 'pkginfo.xml'))
                        extravalues.update(values)
                finally:
                    shutil.rmtree(tmpfdir)
    else:
        # Assume we're pointing to an existing source tree
        if args.extract_to:
            logger.error('--extract-to cannot be specified if source is a directory')
            sys.exit(1)
        if not os.path.isdir(source):
            logger.error('Invalid source directory %s' % source)
            sys.exit(1)
        srctree = source
        srcuri = ''
        if os.path.exists(os.path.join(srctree, '.git')):
            # Try to get upstream repo location from origin remote
            try:
                stdout, _ = bb.process.run('git remote -v', cwd=srctree, shell=True)
            except bb.process.ExecutionError as e:
                stdout = None
            if stdout:
                for line in stdout.splitlines():
                    splitline = line.split()
                    if len(splitline) > 1:
                        if splitline[0] == 'origin' and scriptutils.is_src_url(splitline[1]):
                            srcuri = reformat_git_uri(splitline[1]) + ';branch=master'
                            break

    if args.src_subdir:
        srcsubdir = os.path.join(srcsubdir, args.src_subdir)
        srctree_use = os.path.abspath(os.path.join(srctree, args.src_subdir))
    else:
        srctree_use = os.path.abspath(srctree)

    if args.outfile and os.path.isdir(args.outfile):
        outfile = None
        outdir = args.outfile
    else:
        outfile = args.outfile
        outdir = None
    if outfile and outfile != '-':
        if os.path.exists(outfile):
            logger.error('Output file %s already exists' % outfile)
            sys.exit(1)

    lines_before = []
    lines_after = []

    lines_before.append('# Recipe created by %s' % os.path.basename(sys.argv[0]))
    lines_before.append('# This is the basis of a recipe and may need further editing in order to be fully functional.')
    lines_before.append('# (Feel free to remove these comments when editing.)')
    # We need a blank line here so that patch_recipe_lines can rewind before the LICENSE comments
    lines_before.append('')

    # We'll come back and replace this later in handle_license_vars()
    lines_before.append('##LICENSE_PLACEHOLDER##')


    # FIXME This is kind of a hack, we probably ought to be using bitbake to do this
    pn = None
    pv = None
    if outfile:
        recipefn = os.path.splitext(os.path.basename(outfile))[0]
        fnsplit = recipefn.split('_')
        if len(fnsplit) > 1:
            pn = fnsplit[0]
            pv = fnsplit[1]
        else:
            pn = recipefn

    if args.version:
        pv = args.version

    if args.name:
        pn = args.name
        if args.name.endswith('-native'):
            if args.also_native:
                logger.error('--also-native cannot be specified for a recipe named *-native (*-native denotes a recipe that is already only for native) - either remove the -native suffix from the name or drop --also-native')
                sys.exit(1)
            classes.append('native')
        elif args.name.startswith('nativesdk-'):
            if args.also_native:
                logger.error('--also-native cannot be specified for a recipe named nativesdk-* (nativesdk-* denotes a recipe that is already only for nativesdk)')
                sys.exit(1)
            classes.append('nativesdk')

    if pv and pv not in 'git svn hg'.split():
        realpv = pv
    else:
        realpv = None

    if not srcuri:
        lines_before.append('# No information for SRC_URI yet (only an external source tree was specified)')
    lines_before.append('SRC_URI = "%s"' % srcuri)
    shown_checksums = ["%ssum" % s for s in bb.fetch2.SHOWN_CHECKSUM_LIST]
    for key, value in sorted(checksums.items()):
        if key in shown_checksums:
            lines_before.append('SRC_URI[%s] = "%s"' % (key, value))
    if srcuri and supports_srcrev(srcuri):
        lines_before.append('')
        lines_before.append('# Modify these as desired')
        # Note: we have code to replace realpv further down if it gets set to some other value
        scheme, _, _, _, _, _ = bb.fetch2.decodeurl(srcuri)
        if scheme in ['git', 'gitsm']:
            srcpvprefix = 'git'
        elif scheme == 'svn':
            srcpvprefix = 'svnr'
        else:
            srcpvprefix = scheme
        lines_before.append('PV = "%s+%s"' % (realpv or '1.0', srcpvprefix))
        pv_srcpv = True
        if not args.autorev and srcrev == '${AUTOREV}':
            if os.path.exists(os.path.join(srctree, '.git')):
                (stdout, _) = bb.process.run('git rev-parse HEAD', cwd=srctree)
                srcrev = stdout.rstrip()
        lines_before.append('SRCREV = "%s"' % srcrev)
    if args.provides:
        lines_before.append('PROVIDES = "%s"' % args.provides)
    lines_before.append('')

    if srcsubdir and not args.binary:
        # (for binary packages we explicitly specify subdir= when fetching to
        # match the default value of S, so we don't need to set it in that case)
        lines_before.append('S = "${UNPACKDIR}/%s"' % srcsubdir)
        lines_before.append('')

    if pkgarch:
        lines_after.append('PACKAGE_ARCH = "%s"' % pkgarch)
        lines_after.append('')

    if args.binary:
        lines_after.append('INSANE_SKIP:${PN} += "already-stripped"')
        lines_after.append('')

    if args.npm_dev:
        extravalues['NPM_INSTALL_DEV'] = 1

    # Apply the handlers
    if args.binary:
        classes.append('bin_package')
        handled.append('buildsystem')

    for handler in handlers:
        handler.process(srctree_use, classes, lines_before, lines_after, handled, extravalues)

    # native and nativesdk classes are special and must be inherited last
    # If present, put them at the end of the classes list
    classes.sort(key=lambda c: c in ("native", "nativesdk"))

    extrafiles = extravalues.pop('extrafiles', {})
    extra_pn = extravalues.pop('PN', None)
    extra_pv = extravalues.pop('PV', None)
    run_tasks = extravalues.pop('run_tasks', "").split()

    if extra_pv and not realpv:
        realpv = extra_pv
        if not validate_pv(realpv):
            realpv = None
        else:
            realpv = realpv.lower().split()[0]
            if '_' in realpv:
                realpv = realpv.replace('_', '-')
    if extra_pn and not pn:
        pn = extra_pn
        if pn.startswith('GNU '):
            pn = pn[4:]
        if ' ' in pn:
            # Probably a descriptive identifier rather than a proper name
            pn = None
        else:
            pn = pn.lower()
            if '_' in pn:
                pn = pn.replace('_', '-')

    if srcuri and not realpv or not pn:
        name_pn, name_pv = determine_from_url(srcuri)
        if name_pn and not pn:
            pn = name_pn
        if name_pv and not realpv:
            realpv = name_pv

    licvalues = handle_license_vars(srctree_use, lines_before, handled, extravalues, tinfoil.config_data)

    if not outfile:
        if not pn:
            log_error_cond('Unable to determine short program name from source tree - please specify name with -N/--name or output file name with -o/--outfile', args.devtool)
            # devtool looks for this specific exit code, so don't change it
            sys.exit(15)
        else:
            if srcuri and srcuri.startswith(('gitsm://', 'git://', 'hg://', 'svn://')):
                suffix = srcuri.split(':', 1)[0]
                if suffix == 'gitsm':
                    suffix = 'git'
                outfile = '%s_%s.bb' % (pn, suffix)
            elif realpv:
                outfile = '%s_%s.bb' % (pn, realpv)
            else:
                outfile = '%s.bb' % pn
            if outdir:
                outfile = os.path.join(outdir, outfile)
            # We need to check this again
            if os.path.exists(outfile):
                logger.error('Output file %s already exists' % outfile)
                sys.exit(1)

    # Move any extra files the plugins created to a directory next to the recipe
    if extrafiles:
        if outfile == '-':
            extraoutdir = pn
        else:
            extraoutdir = os.path.join(os.path.dirname(outfile), pn)
        bb.utils.mkdirhier(extraoutdir)
        for destfn, extrafile in extrafiles.items():
            fn = destfn.format(pn=pn, pv=realpv)
            shutil.move(extrafile, os.path.join(extraoutdir, fn))

    lines = lines_before
    lines_before = []
    skipblank = True
    for line in lines:
        if skipblank:
            skipblank = False
            if not line:
                continue
        if line.startswith('S = '):
            if realpv and pv not in 'git svn hg'.split():
                line = line.replace(realpv, '${PV}')
            if pn:
                line = line.replace(pn, '${BPN}')
            if line == 'S = "${UNPACKDIR}/${BPN}-${PV}"' or 'tmp-recipetool-' in line:
                skipblank = True
                continue
        elif line.startswith('SRC_URI = '):
            if realpv and not pv_srcpv:
                line = line.replace(realpv, '${PV}')
        elif line.startswith('PV = '):
            if realpv:
                # Replace the first part of the PV value
                line = re.sub(r'"[^+]*\+', '"%s+' % realpv, line)
        lines_before.append(line)

    if args.also_native:
        lines = lines_after
        lines_after = []
        bbclassextend = None
        for line in lines:
            if line.startswith('BBCLASSEXTEND ='):
                splitval = line.split('"')
                if len(splitval) > 1:
                    bbclassextend = splitval[1].split()
                    if not 'native' in bbclassextend:
                        bbclassextend.insert(0, 'native')
                line = 'BBCLASSEXTEND = "%s"' % ' '.join(bbclassextend)
            lines_after.append(line)
        if not bbclassextend:
            lines_after.append('BBCLASSEXTEND = "native"')

    postinst = ("postinst", extravalues.pop('postinst', None))
    postrm = ("postrm", extravalues.pop('postrm', None))
    preinst = ("preinst", extravalues.pop('preinst', None))
    prerm = ("prerm", extravalues.pop('prerm', None))
    funcs = [postinst, postrm, preinst, prerm]
    for func in funcs:
        if func[1]:
            RecipeHandler.genfunction(lines_after, 'pkg_%s_${PN}' % func[0], func[1])

    outlines = []
    outlines.extend(lines_before)
    if classes:
        if outlines[-1] and not outlines[-1].startswith('#'):
            outlines.append('')
        outlines.append('inherit %s' % ' '.join(classes))
        outlines.append('')
    outlines.extend(lines_after)

    outlines = [ line.rstrip('\n') +"\n" for line in outlines]

    if extravalues:
        _, outlines = oe.recipeutils.patch_recipe_lines(outlines, extravalues, trailing_newline=True)

    if args.extract_to:
        scriptutils.git_convert_standalone_clone(srctree)
        if os.path.isdir(args.extract_to):
            # If the directory exists we'll move the temp dir into it instead of
            # its contents - of course, we could try to always move its contents
            # but that is a pain if there are symlinks; the simplest solution is
            # to just remove it first
            os.rmdir(args.extract_to)
        shutil.move(srctree, args.extract_to)
        if tempsrc == srctree:
            tempsrc = None
        log_info_cond('Source extracted to %s' % args.extract_to, args.devtool)

    if outfile == '-':
        sys.stdout.write(''.join(outlines) + '\n')
    else:
        with open(outfile, 'w') as f:
            lastline = None
            for line in outlines:
                if not lastline and not line:
                    # Skip extra blank lines
                    continue
                f.write('%s' % line)
                lastline = line
        log_info_cond('Recipe %s has been created; further editing may be required to make it fully functional' % outfile, args.devtool)
        tinfoil.modified_files()

    for task in run_tasks:
        logger.info("Running task %s" % task)
        tinfoil.build_file_sync(outfile, task)

    if tempsrc:
        if args.keep_temp:
            logger.info('Preserving temporary directory %s' % tempsrc)
        else:
            shutil.rmtree(tempsrc)

    return 0

def check_single_file(fn, fetchuri):
    """Determine if a single downloaded file is something we can't handle"""
    with open(fn, 'r', errors='surrogateescape') as f:
        if '<html' in f.read(100).lower():
            logger.error('Fetching "%s" returned a single HTML page - check the URL is correct and functional' % fetchuri)
            sys.exit(1)

def split_value(value):
    if isinstance(value, str):
        return value.split()
    else:
        return value

def fixup_license(value):
    # Ensure licenses with OR starts and ends with brackets
    if '|' in value:
        return '(' + value + ')'
    return value

def handle_license_vars(srctree, lines_before, handled, extravalues, d):
    lichandled = [x for x in handled if x[0] == 'license']
    if lichandled:
        # Someone else has already handled the license vars, just return their value
        return lichandled[0][1]

    licvalues = find_licenses(srctree, d)
    licenses = []
    lic_files_chksum = []
    lic_unknown = []
    lines = []
    if licvalues:
        for licvalue in licvalues:
            license = licvalue[0]
            lics = tidy_licenses(fixup_license(license))
            lics = [lic for lic in lics if lic not in licenses]
            if len(lics):
                licenses.extend(lics)
            lic_files_chksum.append('file://%s;md5=%s' % (licvalue[1], licvalue[2]))
            if license == 'Unknown':
                lic_unknown.append(licvalue[1])
        if lic_unknown:
            lines.append('#')
            lines.append('# The following license files were not able to be identified and are')
            lines.append('# represented as "Unknown" below, you will need to check them yourself:')
            for licfile in lic_unknown:
                lines.append('#   %s' % licfile)

    extra_license = tidy_licenses(extravalues.pop('LICENSE', ''))
    if extra_license:
        if licenses == ['Unknown']:
            licenses = extra_license
        else:
            for item in extra_license:
                if item not in licenses:
                    licenses.append(item)
    extra_lic_files_chksum = split_value(extravalues.pop('LIC_FILES_CHKSUM', []))
    for item in extra_lic_files_chksum:
        if item not in lic_files_chksum:
            lic_files_chksum.append(item)

    if lic_files_chksum:
        # We are going to set the vars, so prepend the standard disclaimer
        lines.insert(0, '# WARNING: the following LICENSE and LIC_FILES_CHKSUM values are best guesses - it is')
        lines.insert(1, '# your responsibility to verify that the values are complete and correct.')
    else:
        # Without LIC_FILES_CHKSUM we set LICENSE = "CLOSED" to allow the
        # user to get started easily
        lines.append('# Unable to find any files that looked like license statements. Check the accompanying')
        lines.append('# documentation and source headers and set LICENSE and LIC_FILES_CHKSUM accordingly.')
        lines.append('#')
        lines.append('# NOTE: LICENSE is being set to "CLOSED" to allow you to at least start building - if')
        lines.append('# this is not accurate with respect to the licensing of the software being built (it')
        lines.append('# will not be in most cases) you must specify the correct value before using this')
        lines.append('# recipe for anything other than initial testing/development!')
        licenses = ['CLOSED']

    if extra_license and sorted(licenses) != sorted(extra_license):
        lines.append('# NOTE: Original package / source metadata indicates license is: %s' % ' & '.join(extra_license))

    if len(licenses) > 1:
        lines.append('#')
        lines.append('# NOTE: multiple licenses have been detected; they have been separated with &')
        lines.append('# in the LICENSE value for now since it is a reasonable assumption that all')
        lines.append('# of the licenses apply. If instead there is a choice between the multiple')
        lines.append('# licenses then you should change the value to separate the licenses with |')
        lines.append('# instead of &. If there is any doubt, check the accompanying documentation')
        lines.append('# to determine which situation is applicable.')

    lines.append('LICENSE = "%s"' % ' & '.join(sorted(licenses, key=str.casefold)))
    lines.append('LIC_FILES_CHKSUM = "%s"' % ' \\\n                    '.join(lic_files_chksum))
    lines.append('')

    # Replace the placeholder so we get the values in the right place in the recipe file
    try:
        pos = lines_before.index('##LICENSE_PLACEHOLDER##')
    except ValueError:
        pos = -1
    if pos == -1:
        lines_before.extend(lines)
    else:
        lines_before[pos:pos+1] = lines

    handled.append(('license', licvalues))
    return licvalues

def split_pkg_licenses(licvalues, packages, outlines, fallback_licenses=None, pn='${PN}'):
    """
    Given a list of (license, path, md5sum) as returned by match_licenses(),
    a dict of package name to path mappings, write out a set of
    package-specific LICENSE values.
    """
    pkglicenses = {pn: []}
    for license, licpath, _ in licvalues:
        license = fixup_license(license)
        for pkgname, pkgpath in packages.items():
            if licpath.startswith(pkgpath + '/'):
                if pkgname in pkglicenses:
                    pkglicenses[pkgname].append(license)
                else:
                    pkglicenses[pkgname] = [license]
                break
        else:
            # Accumulate on the main package
            pkglicenses[pn].append(license)
    outlicenses = {}
    for pkgname in packages:
        # Assume AND operator between license files
        license = ' & '.join(list(set(pkglicenses.get(pkgname, ['Unknown'])))) or 'Unknown'
        if license == 'Unknown' and fallback_licenses and pkgname in fallback_licenses:
            license = fallback_licenses[pkgname]
        licenses = tidy_licenses(license)
        license = ' & '.join(licenses)
        outlines.append('LICENSE:%s = "%s"' % (pkgname, license))
        outlicenses[pkgname] = licenses
    return outlicenses

def generate_common_licenses_chksums(common_licenses, d):
    lic_files_chksums = []
    for license in tidy_licenses(common_licenses):
        licfile = '${COMMON_LICENSE_DIR}/' + license
        md5value = bb.utils.md5_file(d.expand(licfile))
        lic_files_chksums.append('file://%s;md5=%s' % (licfile, md5value))
    return lic_files_chksums

def read_pkgconfig_provides(d):
    pkgdatadir = d.getVar('PKGDATA_DIR')
    pkgmap = {}
    for fn in glob.glob(os.path.join(pkgdatadir, 'shlibs2', '*.pclist')):
        with open(fn, 'r') as f:
            for line in f:
                pkgmap[os.path.basename(line.rstrip())] = os.path.splitext(os.path.basename(fn))[0]
    recipemap = {}
    for pc, pkg in pkgmap.items():
        pkgdatafile = os.path.join(pkgdatadir, 'runtime', pkg)
        if os.path.exists(pkgdatafile):
            with open(pkgdatafile, 'r') as f:
                for line in f:
                    if line.startswith('PN: '):
                        recipemap[pc] = line.split(':', 1)[1].strip()
    return recipemap

def convert_debian(debpath):
    value_map = {'Package': 'PN',
                 'Version': 'PV',
                 'Section': 'SECTION',
                 'License': 'LICENSE',
                 'Homepage': 'HOMEPAGE'}

    # FIXME extend this mapping - perhaps use distro_alias.inc?
    depmap = {'libz-dev': 'zlib'}

    values = {}
    depends = []
    with open(os.path.join(debpath, 'control'), 'r', errors='surrogateescape') as f:
        indesc = False
        for line in f:
            if indesc:
                if line.startswith(' '):
                    if line.startswith(' This package contains'):
                        indesc = False
                    else:
                        if 'DESCRIPTION' in values:
                            values['DESCRIPTION'] += ' ' + line.strip()
                        else:
                            values['DESCRIPTION'] = line.strip()
                else:
                    indesc = False
            if not indesc:
                splitline = line.split(':', 1)
                if len(splitline) < 2:
                    continue
                key = splitline[0]
                value = splitline[1].strip()
                if key == 'Build-Depends':
                    for dep in value.split(','):
                        dep = dep.split()[0]
                        mapped = depmap.get(dep, '')
                        if mapped:
                            depends.append(mapped)
                elif key == 'Description':
                    values['SUMMARY'] = value
                    indesc = True
                else:
                    varname = value_map.get(key, None)
                    if varname:
                        values[varname] = value
    postinst = os.path.join(debpath, 'postinst')
    postrm = os.path.join(debpath, 'postrm')
    preinst = os.path.join(debpath, 'preinst')
    prerm = os.path.join(debpath, 'prerm')
    sfiles = [postinst, postrm, preinst, prerm]
    for sfile in sfiles:
        if os.path.isfile(sfile):
            logger.info("Converting %s file to recipe function..." %
                    os.path.basename(sfile).upper())
            content = []
            with open(sfile) as f:
                for line in f:
                    if "#!/" in line:
                        continue
                    line = line.rstrip("\n")
                    if line.strip():
                        content.append(line)
                if content:
                    values[os.path.basename(f.name)] = content

    #if depends:
    #    values['DEPENDS'] = ' '.join(depends)

    return values

def convert_rpm_xml(xmlfile):
    '''Converts the output from rpm -qp --xml to a set of variable values'''
    import xml.etree.ElementTree as ElementTree
    rpmtag_map = {'Name': 'PN',
                  'Version': 'PV',
                  'Summary': 'SUMMARY',
                  'Description': 'DESCRIPTION',
                  'License': 'LICENSE',
                  'Url': 'HOMEPAGE'}

    values = {}
    tree = ElementTree.parse(xmlfile)
    root = tree.getroot()
    for child in root:
        if child.tag == 'rpmTag':
            name = child.attrib.get('name', None)
            if name:
                varname = rpmtag_map.get(name, None)
                if varname:
                    values[varname] = child[0].text
    return values


def register_commands(subparsers):
    parser_create = subparsers.add_parser('create',
                                          help='Create a new recipe',
                                          description='Creates a new recipe from a source tree')
    parser_create.add_argument('source', help='Path or URL to source')
    parser_create.add_argument('-o', '--outfile', help='Specify filename for recipe to create')
    parser_create.add_argument('-p', '--provides', help='Specify an alias for the item provided by the recipe')
    parser_create.add_argument('-m', '--machine', help='Make recipe machine-specific as opposed to architecture-specific', action='store_true')
    parser_create.add_argument('-x', '--extract-to', metavar='EXTRACTPATH', help='Assuming source is a URL, fetch it and extract it to the directory specified as %(metavar)s')
    parser_create.add_argument('-N', '--name', help='Name to use within recipe (PN)')
    parser_create.add_argument('-V', '--version', help='Version to use within recipe (PV)')
    parser_create.add_argument('-b', '--binary', help='Treat the source tree as something that should be installed verbatim (no compilation, same directory structure)', action='store_true')
    parser_create.add_argument('--also-native', help='Also add native variant (i.e. support building recipe for the build host as well as the target machine)', action='store_true')
    parser_create.add_argument('--src-subdir', help='Specify subdirectory within source tree to use', metavar='SUBDIR')
    group = parser_create.add_mutually_exclusive_group()
    group.add_argument('-a', '--autorev', help='When fetching from a git repository, set SRCREV in the recipe to a floating revision instead of fixed', action="store_true")
    group.add_argument('-S', '--srcrev', help='Source revision to fetch if fetching from an SCM such as git (default latest)')
    parser_create.add_argument('-B', '--srcbranch', help='Branch in source repository if fetching from an SCM such as git (default master)')
    parser_create.add_argument('--keep-temp', action="store_true", help='Keep temporary directory (for debugging)')
    parser_create.add_argument('--npm-dev', action="store_true", help='For npm, also fetch devDependencies')
    parser_create.add_argument('--no-pypi', action="store_true", help='Do not inherit pypi class')
    parser_create.add_argument('--devtool', action="store_true", help=argparse.SUPPRESS)
    parser_create.add_argument('--mirrors', action="store_true", help='Enable PREMIRRORS and MIRRORS for source tree fetching (disabled by default).')
    parser_create.set_defaults(func=create_recipe)
