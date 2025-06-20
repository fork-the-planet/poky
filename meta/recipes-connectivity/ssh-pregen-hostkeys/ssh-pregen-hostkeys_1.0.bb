SUMMARY = "Pre generated host keys mainly for speeding up our qemu tests"

SRC_URI = "file://dropbear_rsa_host_key \
           file://openssh"

LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

S = "${UNPACKDIR}"

INHIBIT_DEFAULT_DEPS = "1"

COMPATIBLE_MACHINE = "^qemu.*$"

do_install () {
	install -d ${D}${sysconfdir}/dropbear
	install ${UNPACKDIR}/dropbear_rsa_host_key -m 0600 ${D}${sysconfdir}/dropbear/

	install -d ${D}${sysconfdir}/ssh
	install ${UNPACKDIR}/openssh/* ${D}${sysconfdir}/ssh/
	chmod 0600 ${D}${sysconfdir}/ssh/*
	chmod 0644 ${D}${sysconfdir}/ssh/*.pub
}
