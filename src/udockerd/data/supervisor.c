/* Tiny process supervisor: guarantees the container command's whole
 * process group dies if udockerd dies (crash/SIGKILL/OOM included).
 *
 * Forks instead of exec'ing the command directly: PDEATHSIG only fires
 * on the exact process that set it, and proot forks its own traced child
 * to run the actual container command — that grandchild wouldn't inherit
 * our PDEATHSIG watch. So this process stays alive as a tiny init: sets
 * its own PDEATHSIG to SIGTERM, forks the real command, and waits on it.
 *
 * SIGTERM here can mean either udockerd's own graceful-stop (more signals
 * may follow) or PDEATHSIG firing because udockerd died (nothing else is
 * coming) — can't tell which, so we run our own grace period regardless:
 * forward SIGTERM to the child, wait, then killpg SIGKILL.
 *
 * TTY mode: the child must be its own session leader with no ctty when it
 * opens the pty slave, so it setsid()s again, landing in a *different*
 * pgid than this supervisor. Cleanup must therefore killpg the child's
 * pgid explicitly (before killpg(0, ...), which would otherwise kill us
 * first and skip that second call).
 *
 * Compiled on first use by udockerd (see supervisor.py) — cosmo Python
 * has no ctypes, so PDEATHSIG can't be set in-process.
 *
 * usage: udockerd_supervisor notty <command> [args...]
 *        udockerd_supervisor tty <pty-slave-path> <command> [args...]
 */
#define _DEFAULT_SOURCE
#include <fcntl.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define GRACE_SECONDS 5

static volatile sig_atomic_t g_terminate = 0;

static void on_sigterm(int signo) {
    (void)signo;
    g_terminate = 1;
}

static double monotonic_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

int main(int argc, char *argv[]) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s notty <command> [args...]\n", argv[0]);
        fprintf(stderr, "       %s tty <pty-slave-path> <command> [args...]\n", argv[0]);
        return 2;
    }

    int is_tty = strcmp(argv[1], "tty") == 0;
    const char *pty_slave_path = NULL;
    char **cmd_argv;

    if (is_tty) {
        if (argc < 4) {
            fprintf(stderr, "usage: %s tty <pty-slave-path> <command> [args...]\n", argv[0]);
            return 2;
        }
        pty_slave_path = argv[2];
        cmd_argv = &argv[3];
    } else {
        cmd_argv = &argv[2];
    }

    if (setsid() < 0) {
        /* not fatal: happens if we're already a session/group leader */
    }

    if (prctl(PR_SET_PDEATHSIG, SIGTERM) != 0) {
        perror("prctl(PR_SET_PDEATHSIG)");
        return 2;
    }

    struct sigaction sa;
    sa.sa_handler = on_sigterm;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGTERM, &sa, NULL);

    pid_t child = fork();
    if (child < 0) {
        perror("fork");
        return 2;
    }

    if (child == 0) {
        if (is_tty) {
            /* New session, no ctty yet; opening the pty slave assigns it. */
            setsid();
            int slave_fd = open(pty_slave_path, O_RDWR);
            if (slave_fd < 0) {
                perror("open pty slave");
                _exit(126);
            }
            dup2(slave_fd, 0);
            dup2(slave_fd, 1);
            dup2(slave_fd, 2);
            if (slave_fd > 2) {
                close(slave_fd);
            }
        }
        execvp(cmd_argv[0], cmd_argv);
        perror("execvp");
        _exit(127);
    }

    /* Parent: reap child, watch for our own termination signal, escalate
     * SIGTERM -> grace period -> SIGKILL. */
    int status = 0;
    int terminating = 0;
    double deadline = 0;
    for (;;) {
        pid_t reaped = waitpid(child, &status, WNOHANG);
        if (reaped == child) {
            break;
        }
        if (reaped < 0) {
            break;
        }
        if (g_terminate && !terminating) {
            terminating = 1;
            deadline = monotonic_now() + GRACE_SECONDS;
            kill(child, SIGTERM);
        }
        if (terminating && monotonic_now() >= deadline) {
            if (is_tty) {
                /* Child has its own pgid in TTY mode; kill it before our
                 * own pgid below, since that call kills us too. */
                killpg(child, SIGKILL);
            }
            killpg(0, SIGKILL);
        }
        usleep(50 * 1000);
    }

    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 1;
}
