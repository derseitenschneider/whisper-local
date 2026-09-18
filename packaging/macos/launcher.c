// Bundle executable for Whisper Local.app. It spawns the installed
// `whisper-local` as a child and waits, rather than exec'ing it: macOS
// attributes a process's privacy permissions (Microphone, Accessibility,
// Input Monitoring) to its responsible app, and children inherit that. An exec
// would turn this process into Homebrew's Python.app, and the grants would show
// up as "python3" instead of "Whisper Local".
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

#if !defined(WL_BIN) || !defined(WL_APP)
#error "compile with -DWL_BIN=\"/path/to/whisper-local\" -DWL_APP=\"/path/to/Whisper Local.app\""
#endif

static pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(int argc, char **argv) {
    (void)argc;
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);

    const char *home = getenv("HOME");
    if (!home) home = "/tmp";

    // GUI launches get a bare PATH; add Homebrew and ~/.local/bin for helpers (ollama, ffplay).
    char path[4096];
    snprintf(path, sizeof path, "/opt/homebrew/bin:/usr/local/bin:%s/.local/bin:%s",
             home, getenv("PATH") ? getenv("PATH") : "/usr/bin:/bin");
    setenv("PATH", path, 1);

    // No stdin makes sys.stdin None, which the app treats as "no console" and
    // skips its interactive terminal prompts. stdout/stderr go to a log file.
    char log[1024];
    snprintf(log, sizeof log, "%s/.whisperkey/launcher.log", home);
    close(0);
    if (freopen(log, "a", stdout)) {
        dup2(fileno(stdout), 2);
        setvbuf(stdout, NULL, _IOLBF, 0);
    }

    // Lets the app's Restart relaunch this bundle instead of bare Python.
    setenv("WHISPER_LOCAL_APP", WL_APP, 1);

    argv[0] = WL_BIN;  // pass through any `open --args` flags
    if (posix_spawn(&child, WL_BIN, NULL, NULL, argv, environ) != 0) {
        perror("posix_spawn " WL_BIN);
        return 1;
    }

    int status = 0;
    while (waitpid(child, &status, 0) < 0) {}
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
