SUMMARY = "GNU Privacy Guard - encryption and signing tools (2.x)"
DESCRIPTION = "A complete and free implementation of the OpenPGP standard \
as defined by RFC4880 (also known as PGP). GnuPG allows you to encrypt \
and sign your data and communications; it features a versatile key \
management system, along with access modules for all kinds of public \
key directories."
HOMEPAGE = "http://www.gnupg.org/"
LICENSE = "GPL-3.0-only & LGPL-3.0-only"
LIC_FILES_CHKSUM = "file://COPYING;md5=189af8afca6d6075ba6c9e0aa8077626 \
                    file://COPYING.LGPL3;md5=a2b6bf2cb38ee52619e60f30a1fc7257"

DEPENDS = "npth libassuan libksba zlib bzip2 readline libgcrypt"

inherit autotools gettext texinfo pkgconfig upstream-version-is-even

require drop-unknown-suffix.inc

UPSTREAM_CHECK_URI = "https://gnupg.org/ftp/gcrypt/gnupg/"
SRC_URI = "${GNUPG_MIRROR}/${BPN}/${BPN}-${PV}.tar.bz2 \
           file://0002-use-pkgconfig-instead-of-npth-config.patch \
           file://0001-Woverride-init-is-not-needed-with-gcc-9.patch \
           "
SRC_URI:append:class-native = " file://0001-configure.ac-use-a-custom-value-for-the-location-of-.patch \
                                file://relocate.patch"
SRC_URI:append:class-nativesdk = " file://relocate.patch"

SRC_URI[sha256sum] = "7afa71d72ff9aaff75a6810b87b486bc492fd752e4f77b07c41759ce4ef36b31"

EXTRA_OECONF = "--disable-ldap \
		--disable-ccid-driver \
		--with-zlib=${STAGING_LIBDIR}/.. \
		--with-bzip2=${STAGING_LIBDIR}/.. \
		--with-readline=${STAGING_LIBDIR}/.. \
		--with-mailprog=${sbindir}/sendmail \
		--disable-tests \
		--disable-doc \
               "

# A minimal package containing just enough to run gpg+gpgagent (E.g. use gpgme in opkg)
PACKAGES =+ "${PN}-gpg"
FILES:${PN}-gpg = " \
	${bindir}/gpg \
	${bindir}/gpg-agent \
"

# Normal package (gnupg) should depend on minimal package (gnupg-gpg)
# to ensure all tools are included. This is done only in non-native
# builds. Native builds don't have sub-packages, so appending RDEPENDS
# in this case breaks recipe parsing.
RDEPENDS:${PN} += "${@ "" if ("native" in d.getVar("PN")) else (d.getVar("PN") + "-gpg")}"

RRECOMMENDS:${PN} = "pinentry"

do_configure:prepend () {
	# Else these could be used in prefernce to those in aclocal-copy
	rm -f ${S}/m4/gpg-error.m4
	rm -f ${S}/m4/libassuan.m4
	rm -f ${S}/m4/ksba.m4
	rm -f ${S}/m4/libgcrypt.m4
}

do_install:append:class-native() {
	create_wrappers ${STAGING_BINDIR_NATIVE}
}

do_install:append:class-nativesdk() {
	create_wrappers ${SDKPATHNATIVE}${bindir_nativesdk}
}

create_wrappers() {
	for i in gpg gpgconf gpg-agent gpg-connect-agent; do
		create_wrapper ${D}${bindir}/$i GNUPG_BINDIR=$1
	done
}

PACKAGECONFIG ??= "gnutls"
PACKAGECONFIG[gnutls] = "--enable-gnutls, --disable-gnutls, gnutls"
PACKAGECONFIG[sqlite3] = "--enable-sqlite, --disable-sqlite, sqlite3"

BBCLASSEXTEND = "native nativesdk"

lcl_maybe_fortify:mipsarch = ""

CVE_STATUS[CVE-2022-3219] = "upstream-wontfix: Upstream doesn't seem to be keen on merging the proposed commit - https://dev.gnupg.org/T5993"
