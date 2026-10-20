/* Tiny stand-in for a protected database. The binary is named `postgres`, so
 * /proc/<pid>/comm is `postgres` and WhatThePort treats the listener as protected.
 * SIGTERM is ignored; a correct daemon never signals this process at all.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static void daemonize(void) {
    pid_t pid = fork();
    if (pid < 0) _exit(1);
    if (pid > 0) _exit(0);
    if (setsid() < 0) _exit(1);
    pid = fork();
    if (pid < 0) _exit(1);
    if (pid > 0) _exit(0);
}

int main(void) {
    const char *port_s = getenv("PORT");
    const char *pidfile = getenv("PIDFILE");
    if (!port_s) return 2;
    daemonize();

    signal(SIGTERM, SIG_IGN);
    signal(SIGINT, SIG_IGN);

    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return 1;
    int opt = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof opt);

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((unsigned short)atoi(port_s));
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) != 0) return 1;
    if (listen(fd, 16) != 0) return 1;

    if (pidfile) {
        FILE *f = fopen(pidfile, "w");
        if (f) {
            fprintf(f, "%d\n", (int)getpid());
            fclose(f);
        }
    }
    for (;;) pause();
    return 0;
}
