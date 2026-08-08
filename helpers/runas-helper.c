/* SPDX-License-Identifier: GPL-3.0-only */
#include <windows.h>
#include <shellapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void append_quoted(char **out, size_t *left, const char *arg)
{
    size_t backslashes = 0;
    int quote = !*arg || strpbrk(arg, " \t\"") != NULL;
    if (quote && *left > 1) { *(*out)++ = '"'; --*left; }
    for (; *arg && *left > 1; ++arg) {
        if (*arg == '\\') { ++backslashes; continue; }
        if (*arg == '"') {
            while (backslashes-- && *left > 2) { *(*out)++ = '\\'; *(*out)++ = '\\'; *left -= 2; }
            if (*left > 2) { *(*out)++ = '\\'; *(*out)++ = '"'; *left -= 2; }
        } else {
            while (backslashes-- && *left > 1) { *(*out)++ = '\\'; --*left; }
            *(*out)++ = *arg; --*left;
        }
        backslashes = 0;
    }
    while (backslashes-- && *left > (quote ? 2u : 1u)) {
        *(*out)++ = '\\'; --*left;
        if (quote) { *(*out)++ = '\\'; --*left; }
    }
    if (quote && *left > 1) { *(*out)++ = '"'; --*left; }
    **out = '\0';
}

int main(int argc, char **argv)
{
    SHELLEXECUTEINFOA info = {0};
    char *params, *cursor;
    size_t size = 1, left;
    DWORD exit_code = 1;
    int i;

    if (argc < 2) {
        fprintf(stderr, "Usage: runas-helper.exe PROGRAM [ARGUMENT ...]\n");
        return 2;
    }
    for (i = 2; i < argc; ++i) size += strlen(argv[i]) * 2 + 4;
    params = calloc(size, 1);
    if (!params) return 3;
    cursor = params; left = size;
    for (i = 2; i < argc; ++i) {
        if (i > 2 && left > 1) { *cursor++ = ' '; --left; }
        append_quoted(&cursor, &left, argv[i]);
    }

    info.cbSize = sizeof(info);
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC;
    info.lpVerb = "runas";
    info.lpFile = argv[1];
    info.lpParameters = params;
    info.nShow = SW_SHOWNORMAL;
    if (!ShellExecuteExA(&info)) {
        fprintf(stderr, "Run as administrator failed (Windows error %lu)\n", GetLastError());
        free(params);
        return 4;
    }
    free(params);
    if (!info.hProcess) return 0;
    WaitForSingleObject(info.hProcess, INFINITE);
    GetExitCodeProcess(info.hProcess, &exit_code);
    CloseHandle(info.hProcess);
    return (int)exit_code;
}
