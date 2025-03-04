SUMMARY = "Lossless data compressor based on the LZMA algorithm"
HOMEPAGE = "http://lzip.nongnu.org/lzip.html"
DESCRIPTION = "Lzip is a lossless data compressor with a user interface similar to the one of gzip or bzip2. Lzip uses a simplified form of the Lempel-Ziv-Markov chain-Algorithm (LZMA) stream format, chosen to maximize safety and interoperability."
SECTION = "console/utils"
LICENSE = "GPL-2.0-or-later"
LIC_FILES_CHKSUM = "file://COPYING;md5=cca7f74ec83b7a9ce7ccd195aad471bd \
                    file://decoder.cc;beginline=3;endline=16;md5=18c279060cd0be128188404b45837f88 \
                    "

SRC_URI = "${SAVANNAH_GNU_MIRROR}/lzip/lzip-${PV}.tar.gz"
SRC_URI[sha256sum] = "09418a6d8fb83f5113f5bd856e09703df5d37bae0308c668d0f346e3d3f0a56f"

B = "${WORKDIR}/build"
do_configure[cleandirs] = "${B}"

CONFIGUREOPTS = "\
    '--srcdir=${S}' \
    '--prefix=${prefix}' \
    '--exec-prefix=${exec_prefix}' \
    '--bindir=${bindir}' \
    '--datadir=${datadir}' \
    '--infodir=${infodir}' \
    '--sysconfdir=${sysconfdir}' \
    'CXX=${CXX}' \
    'CPPFLAGS=${CPPFLAGS}' \
    'CXXFLAGS=${CXXFLAGS}' \
    'LDFLAGS=${LDFLAGS}' \
"

do_configure () {
    ${S}/configure ${CONFIGUREOPTS}
}

do_install () {
    oe_runmake 'DESTDIR=${D}' install
    # Info dir listing isn't interesting at this point so remove it if it exists.
    if [ -e "${D}${infodir}/dir" ]; then
        rm -f ${D}${infodir}/dir
    fi
}

BBCLASSEXTEND = "native nativesdk"
