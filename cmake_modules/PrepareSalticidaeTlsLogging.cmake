function(
    kauri_prepare_salticidae_tls_logging
    salticidae_source_dir
    overlay_dir
    output_conn_source
    output_include_dir)
    set(conn_source "${salticidae_source_dir}/src/conn.cpp")
    set(util_header "${salticidae_source_dir}/include/salticidae/util.h")
    set_property(
        DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
        "${conn_source}"
        "${util_header}")
    if(NOT EXISTS "${conn_source}" OR NOT EXISTS "${util_header}")
        message(FATAL_ERROR
            "Pinned Salticidae TLS logging sources are unavailable")
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

    set(${output_conn_source} "${overlay_conn}" PARENT_SCOPE)
    set(${output_include_dir} "${overlay_dir}/include" PARENT_SCOPE)
endfunction()
