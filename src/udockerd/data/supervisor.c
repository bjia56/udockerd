/* Tiny process supervisor: runs a command in its own process group and
 * guarantees the whole group is killed if udockerd itself dies, even via
 * SIGKILL/crash/OOM, when a plain PR_SET_PDEATHSIG on the command itself
 * wouldn't be enough.
 *
 * Why fork() instead of a plain exec wrapper: PDEATHSIG only fires on the
 * exact process that set it. udocker's proot engine forks its own traced
 * child to run the actual container command — that fork does not inherit
 * an active PDEATHSIG watch, so if this process just exec'd proot
 * directly, proot itself would die correctly when udockerd dies, but
 * proot's child (the real container command) would be orphaned to init
 * and keep running. Since SIGKILL never runs cleanup code in the dying
 * process, proot's own --kill-on-exit handler can't save us either.
 *
 * So instead this process forks: the child execs the real command (proot
 * ...), and this process stays alive as a tiny init for that subtree —
 * it sets its own PDEATHSIG to SIGTERM (catchable, unlike SIGKILL) so it
 * gets a chance to run cleanup, and waits on its child (reaping zombies
 * from anything double-forked underneath, same as a real init would).
 *
 * SIGTERM here means one of two things: udockerd sent it directly as
 * part of its own graceful-stop path (daemon still alive, will follow up
 * with SIGKILL after its own grace period if needed), or PDEATHSIG fired
 * because udockerd died (daemon gone, no follow-up signal is coming from
 * anywhere). Either way this process can't tell which case it's in, so
 * it manages a short grace period itself: forward SIGTERM to the child,
 * wait, then killpg SIGKILL if it hasn't exited — giving the real
 * container command a chance to shut down cleanly in both cases, while
 * still guaranteeing termination if the daemon is gone and nothing else
 * will ever send the follow-up kill. This only catches descendants that
 * stayed in this process's group; a process inside the container that
 * calls setsid() itself would still escape, same residual gap a real
 * init has for that case.
 *
 * Compiled on first use by udockerd (see supervisor.py) since cosmo
 * Python has no ctypes, so PDEATHSIG can't be set in-process via FFI.
 *
 * usage: udockerd_supervisor <command> [args...]
 */
#define _DEFAULT_SOURCE
#include <signal.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
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
    if (argc < 2) {
        fprintf(stderr, "usage: %s <command> [args...]\n", argv[0]);
        return 2;
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
        /* Child: exec the real command. It inherits our pgid (no further
         * setsid here), so a killpg from the supervisor reaches it. */
        execvp(argv[1], &argv[1]);
        perror("execvp");
        _exit(127);
    }

    /* Parent: supervise. Loop on waitpid so we reap the direct child
     * (and, if it double-forks without its own setsid, other
     * descendants) while watching for our own termination signal.
     *
     * SIGTERM (from udockerd's graceful stop, or PDEATHSIG firing because
     * udockerd died) forwards SIGTERM to the child and starts a grace
     * period timer here — this process manages the escalation itself
     * since it can't tell whether a live udockerd will still send a
     * follow-up SIGKILL or whether it's already gone. */
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
            killpg(0, SIGKILL);
            /* Our own pgid includes us; SIGKILL here ends this loop too. */
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
