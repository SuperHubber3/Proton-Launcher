/* SPDX-License-Identifier: GPL-3.0-only */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#define BUFFER_LENGTH 32768

static WCHAR target[BUFFER_LENGTH];
static WCHAR arguments[BUFFER_LENGTH];
static WCHAR directory[BUFFER_LENGTH];
static WCHAR overrides[BUFFER_LENGTH];
static WCHAR steam_app_id[64];
static WCHAR command_line[BUFFER_LENGTH];
static WCHAR has_overrides[2];

void WINAPI mainCRTStartup(void)
{
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    DWORD target_length;
    DWORD arguments_length;
    DWORD directory_length;
    DWORD overrides_length;
    DWORD steam_id_length;
    DWORD cursor = 0;
    DWORD i;

    target_length = GetEnvironmentVariableW(
        L"PL_STEAM_RETRY_TARGET", target, BUFFER_LENGTH);
    if (target_length == 0 || target_length >= BUFFER_LENGTH)
        ExitProcess(2);

    arguments_length = GetEnvironmentVariableW(
        L"PL_STEAM_RETRY_ARGUMENTS", arguments, BUFFER_LENGTH);
    if (arguments_length >= BUFFER_LENGTH)
        ExitProcess(3);

    directory_length = GetEnvironmentVariableW(
        L"PL_STEAM_RETRY_DIRECTORY", directory, BUFFER_LENGTH);
    if (directory_length >= BUFFER_LENGTH)
        ExitProcess(4);

    overrides_length = GetEnvironmentVariableW(
        L"PL_STEAM_RETRY_WINEDLLOVERRIDES", overrides, BUFFER_LENGTH);
    if (overrides_length >= BUFFER_LENGTH)
        ExitProcess(5);
    if (GetEnvironmentVariableW(
            L"PL_STEAM_RETRY_HAS_WINEDLLOVERRIDES", has_overrides, 2))
        SetEnvironmentVariableW(L"WINEDLLOVERRIDES", overrides);
    else
        SetEnvironmentVariableW(L"WINEDLLOVERRIDES", NULL);

    steam_id_length = GetEnvironmentVariableW(
        L"PL_STEAM_RETRY_STEAM_APP_ID", steam_app_id, 64);
    if (steam_id_length > 0 && steam_id_length < 64) {
        SetEnvironmentVariableW(L"SteamAppId", steam_app_id);
        SetEnvironmentVariableW(L"SteamGameId", steam_app_id);
        SetEnvironmentVariableW(L"SteamOverlayGameId", steam_app_id);
    }

    if (target_length + arguments_length + 4 >= BUFFER_LENGTH)
        ExitProcess(6);
    command_line[cursor++] = L'"';
    for (i = 0; i < target_length; ++i)
        command_line[cursor++] = target[i];
    command_line[cursor++] = L'"';
    if (arguments_length) {
        command_line[cursor++] = L' ';
        for (i = 0; i < arguments_length; ++i)
            command_line[cursor++] = arguments[i];
    }
    command_line[cursor] = L'\0';

    SetEnvironmentVariableW(L"PL_STEAM_RETRY_TARGET", NULL);
    SetEnvironmentVariableW(L"PL_STEAM_RETRY_ARGUMENTS", NULL);
    SetEnvironmentVariableW(L"PL_STEAM_RETRY_DIRECTORY", NULL);
    SetEnvironmentVariableW(L"PL_STEAM_RETRY_WINEDLLOVERRIDES", NULL);
    SetEnvironmentVariableW(L"PL_STEAM_RETRY_HAS_WINEDLLOVERRIDES", NULL);
    SetEnvironmentVariableW(L"PL_STEAM_RETRY_STEAM_APP_ID", NULL);
    startup.cb = sizeof(startup);
    if (!CreateProcessW(
            target,
            command_line,
            NULL,
            NULL,
            FALSE,
            CREATE_NEW_PROCESS_GROUP,
            NULL,
            directory_length ? directory : NULL,
            &startup,
            &process))
        ExitProcess(GetLastError());

    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    ExitProcess(0);
}
