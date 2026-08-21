function(
    kauri_prepare_salticidae_tls_logging
    salticidae_source_dir
    overlay_dir
    output_conn_source
    output_include_dir)
    set(conn_source "${salticidae_source_dir}/src/conn.cpp")
    set(util_header "${salticidae_source_dir}/include/salticidae/util.h")
    set(buffer_header "${salticidae_source_dir}/include/salticidae/buffer.h")
    set(conn_header "${salticidae_source_dir}/include/salticidae/conn.h")
    set(network_header "${salticidae_source_dir}/include/salticidae/network.h")
    set(priority_send_patch
        "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/SalticidaePrioritySend.patch")
    set_property(
        DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
        "${conn_source}"
        "${util_header}"
        "${buffer_header}"
        "${conn_header}"
        "${network_header}"
        "${priority_send_patch}")
    if(NOT EXISTS "${conn_source}" OR NOT EXISTS "${util_header}" OR
       NOT EXISTS "${buffer_header}" OR NOT EXISTS "${conn_header}" OR
       NOT EXISTS "${network_header}" OR NOT EXISTS "${priority_send_patch}")
        message(FATAL_ERROR
            "Pinned Salticidae overlay sources are unavailable")
    endif()

    file(SHA256 "${conn_source}" conn_sha256)
    if(NOT conn_sha256 STREQUAL
       "789c7b72fdba47c3131a1a3eb28cc6511ec26a215fa6d973697372704201729a")
        message(FATAL_ERROR
            "Pinned Salticidae conn.cpp changed; audit the TLS logging overlay")
    endif()
    file(SHA256 "${util_header}" util_sha256)
    if(NOT util_sha256 STREQUAL
       "cd59f6851c189dbbc55ef15cb6fcdc116e14ef4db7d5cb3cdf57cbb72a1aa046")
        message(FATAL_ERROR
            "Pinned Salticidae util.h changed; audit the format-check overlay")
    endif()
    file(SHA256 "${buffer_header}" buffer_sha256)
    if(NOT buffer_sha256 STREQUAL
       "16d4f209e4f7aefa58a6cc04cc05c4edc2be1af93627011c0f251c041b73e18c")
        message(FATAL_ERROR
            "Pinned Salticidae buffer.h changed; audit the priority-send overlay")
    endif()
    file(SHA256 "${conn_header}" conn_header_sha256)
    if(NOT conn_header_sha256 STREQUAL
       "6cb9c8f46104dd84367cc9d3b99841c94275f0b14a497ffff8e2a3264a13ae62")
        message(FATAL_ERROR
            "Pinned Salticidae conn.h changed; audit the priority-send overlay")
    endif()
    file(SHA256 "${network_header}" network_sha256)
    if(NOT network_sha256 STREQUAL
       "4610fb0474e328ce3d7cbbe78e33bc2d18a04a6e20f58c5f7065e13130bf45da")
        message(FATAL_ERROR
            "Pinned Salticidae network.h changed; audit the priority-send overlay")
    endif()

    file(READ "${conn_source}" conn_contents)
    set(broken_tls_log
        "SALTICIDAE_LOG_INFO(\"recv(%d) failure: %d %s\", fd, err, errno);")
    set(fixed_tls_log
        "SALTICIDAE_LOG_INFO(\"ssl recv(%d) failure: %d %d\", fd, err, errno);")
    string(REPLACE
        "${broken_tls_log}"
        "${fixed_tls_log}"
        patched_conn_contents
        "${conn_contents}")
    if(patched_conn_contents STREQUAL conn_contents OR
       patched_conn_contents MATCHES "failure: %d %s.*errno")
        message(FATAL_ERROR
            "Pinned Salticidae TLS receive logging patch did not apply exactly")
    endif()

    file(READ "${util_header}" util_contents)
    set(logger_anchor [=[
extern const char *TTY_COLOR_RESET;

class Logger {]=])
    set(logger_with_format_macro [=[
extern const char *TTY_COLOR_RESET;

#if defined(__GNUC__) || defined(__clang__)
#define SALTICIDAE_PRINTF_LIKE(format_index, first_argument) \
    __attribute__((format(printf, format_index, first_argument)))
#else
#define SALTICIDAE_PRINTF_LIKE(format_index, first_argument)
#endif

class Logger {]=])
    string(REPLACE
        "${logger_anchor}"
        "${logger_with_format_macro}"
        patched_util_contents
        "${util_contents}")
    set(logger_declarations [=[
    void info(const char *fmt, ...);
    void debug(const char *fmt, ...);
    void warning(const char *fmt, ...);
    void error(const char *fmt, ...);
    bool is_tty() { return isatty(output); }
};

extern Logger logger;]=])
    set(checked_logger_declarations [=[
    void info(const char *fmt, ...) SALTICIDAE_PRINTF_LIKE(2, 3);
    void debug(const char *fmt, ...) SALTICIDAE_PRINTF_LIKE(2, 3);
    void warning(const char *fmt, ...) SALTICIDAE_PRINTF_LIKE(2, 3);
    void error(const char *fmt, ...) SALTICIDAE_PRINTF_LIKE(2, 3);
    bool is_tty() { return isatty(output); }
};

#undef SALTICIDAE_PRINTF_LIKE

extern Logger logger;]=])
    string(REPLACE
        "${logger_declarations}"
        "${checked_logger_declarations}"
        patched_util_contents
        "${patched_util_contents}")
    if(patched_util_contents STREQUAL util_contents OR
       NOT patched_util_contents MATCHES
           "__attribute__\\(\\(format\\(printf, format_index, first_argument\\)\\)\\)")
        message(FATAL_ERROR
            "Pinned Salticidae Logger format-check overlay did not apply exactly")
    endif()

    set(overlay_conn "${overlay_dir}/src/conn.cpp")
    set(overlay_util "${overlay_dir}/include/salticidae/util.h")
    set(overlay_buffer "${overlay_dir}/include/salticidae/buffer.h")
    set(overlay_conn_header "${overlay_dir}/include/salticidae/conn.h")
    set(overlay_network "${overlay_dir}/include/salticidae/network.h")
    file(MAKE_DIRECTORY
        "${overlay_dir}/src"
        "${overlay_dir}/include/salticidae")

    set(write_conn TRUE)
    if(EXISTS "${overlay_conn}")
        file(READ "${overlay_conn}" existing_conn_contents)
        if(existing_conn_contents STREQUAL patched_conn_contents)
            set(write_conn FALSE)
        endif()
    endif()
    if(write_conn)
        file(WRITE "${overlay_conn}" "${patched_conn_contents}")
    endif()

    set(write_util TRUE)
    if(EXISTS "${overlay_util}")
        file(READ "${overlay_util}" existing_util_contents)
        if(existing_util_contents STREQUAL patched_util_contents)
            set(write_util FALSE)
        endif()
    endif()
    if(write_util)
        file(WRITE "${overlay_util}" "${patched_util_contents}")
    endif()

    file(COPY_FILE "${buffer_header}" "${overlay_buffer}" ONLY_IF_DIFFERENT)
    file(COPY_FILE "${conn_header}" "${overlay_conn_header}" ONLY_IF_DIFFERENT)
    file(COPY_FILE "${network_header}" "${overlay_network}" ONLY_IF_DIFFERENT)
    find_program(KAURI_PATCH_EXECUTABLE patch REQUIRED)
    execute_process(
        COMMAND "${KAURI_PATCH_EXECUTABLE}" --dry-run --batch --forward
            --ignore-whitespace
            -p1 -i "${priority_send_patch}"
        WORKING_DIRECTORY "${overlay_dir}"
        RESULT_VARIABLE priority_patch_check
        OUTPUT_VARIABLE priority_patch_check_out
        ERROR_VARIABLE priority_patch_check_err)
    if(NOT priority_patch_check EQUAL 0)
        message(FATAL_ERROR
            "Pinned Salticidae priority-send patch check failed: "
            "${priority_patch_check_out}${priority_patch_check_err}")
    endif()
    execute_process(
        COMMAND "${KAURI_PATCH_EXECUTABLE}" --batch --forward
            --ignore-whitespace
            -p1 -i "${priority_send_patch}"
        WORKING_DIRECTORY "${overlay_dir}"
        RESULT_VARIABLE priority_patch_result
        OUTPUT_VARIABLE priority_patch_out
        ERROR_VARIABLE priority_patch_err)
    if(NOT priority_patch_result EQUAL 0)
        message(FATAL_ERROR
            "Pinned Salticidae priority-send patch failed: "
            "${priority_patch_out}${priority_patch_err}")
    endif()

    set(${output_conn_source} "${overlay_conn}" PARENT_SCOPE)
    set(${output_include_dir} "${overlay_dir}/include" PARENT_SCOPE)
endfunction()
