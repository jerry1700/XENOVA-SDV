# Use the console's display name in the existing SSH login message.
do_install:append() {
    printf 'Welcome to %s.\n' "${DISTRO_NAME}" > "${D}${sysconfdir}/motd"
}
