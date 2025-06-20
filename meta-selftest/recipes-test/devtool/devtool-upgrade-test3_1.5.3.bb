SUMMARY = "Pipe viewer test recipe for devtool upgrade test"
LICENSE = "Artistic-2.0"
LIC_FILES_CHKSUM = "file://doc/COPYING;md5=9c50db2589ee3ef10a9b7b2e50ce1d02"

SRC_URI = "http://www.ivarch.com/programs/sources/pv-${PV}.tar.gz"
UPSTREAM_CHECK_URI = "http://www.ivarch.com/programs/pv.shtml"
RECIPE_NO_UPDATE_REASON = "This recipe is used to test devtool upgrade feature"

SRC_URI[md5sum] = "9365d86bd884222b4bf1039b5a9ed1bd"

S = "${UNPACKDIR}/pv-${PV}"

EXCLUDE_FROM_WORLD = "1"

inherit autotools

