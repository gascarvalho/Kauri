if(NOT DEFINED RELIC_SOURCE_DIR)
    message(FATAL_ERROR "RELIC_SOURCE_DIR is required")
endif()

set(blake2_header "${RELIC_SOURCE_DIR}/src/md/blake2.h")
if(NOT EXISTS "${blake2_header}")
    message(FATAL_ERROR "RELIC BLAKE2 header not found: ${blake2_header}")
endif()

file(READ "${blake2_header}" contents)

set(pack_marker "KAURI_RELIC_BLAKE2_PACK_SCOPE")
set(alignment_marker "KAURI_RELIC_BLAKE2_ARM64_ALIGNMENT")
if(NOT contents MATCHES "${pack_marker}")
    set(original "${contents}")
    string(REPLACE
        "  ALIGNME( 64 ) typedef struct __blake2s_state"
        "/* ${pack_marker}: packing applies to wire parameters, not state. */\n#pragma pack(pop)\n  ALIGNME( 64 ) typedef struct __blake2s_state"
        contents "${contents}")
    string(REPLACE
        "  typedef struct __blake2b_param"
        "#pragma pack(push, 1)\n  typedef struct __blake2b_param"
        contents "${contents}")
    string(REPLACE
        "  ALIGNME( 64 ) typedef struct __blake2b_state"
        "#pragma pack(pop)\n  ALIGNME( 64 ) typedef struct __blake2b_state"
        contents "${contents}")
    string(REPLACE
        "  } blake2bp_state;\n#pragma pack(pop)"
        "  } blake2bp_state;"
        contents "${contents}")
    if(contents STREQUAL original OR NOT contents MATCHES "${pack_marker}")
        message(FATAL_ERROR
            "Pinned RELIC BLAKE2 packing changed; portability patch failed")
    endif()
endif()

# AppleClang 17 rejects arrays whose old RELIC BLAKE2 state typedefs have an
# explicit 64-byte alignment but a size that is not a multiple of 64. These
# reference implementations do not require over-aligned state storage.
if(NOT contents MATCHES "${alignment_marker}")
    set(original "${contents}")
    string(REPLACE
        "  ALIGNME( 64 ) typedef struct __blake2s_state"
        "  /* ${alignment_marker} */\n  typedef struct __blake2s_state"
        contents "${contents}")
    string(REPLACE
        "  ALIGNME( 64 ) typedef struct __blake2b_state"
        "  typedef struct __blake2b_state"
        contents "${contents}")
    if(contents STREQUAL original OR NOT contents MATCHES "${alignment_marker}")
        message(FATAL_ERROR
            "Pinned RELIC BLAKE2 alignment changed; portability patch failed")
    endif()
endif()

file(WRITE "${blake2_header}" "${contents}")

# macOS declares err_get_code as a function-like macro. RELIC 0.5.0 uses the
# same name for a function, so undefine the platform macro before RELIC's API
# declaration without changing RELIC's exported symbol names.
set(error_header "${RELIC_SOURCE_DIR}/include/relic_err.h")
if(NOT EXISTS "${error_header}")
    message(FATAL_ERROR "RELIC error header not found: ${error_header}")
endif()

file(READ "${error_header}" error_contents)
set(error_marker "KAURI_RELIC_MACOS_ERR_GET_CODE")
if(NOT error_contents MATCHES "${error_marker}")
    set(original "${error_contents}")
    string(REPLACE
        "#include \"relic_label.h\""
        "#include \"relic_label.h\"\n\n/* ${error_marker} */\n#ifdef err_get_code\n#undef err_get_code\n#endif"
        error_contents "${error_contents}")
    if(error_contents STREQUAL original OR
       NOT error_contents MATCHES "${error_marker}")
        message(FATAL_ERROR
            "Pinned RELIC error API changed; portability patch failed")
    endif()
    file(WRITE "${error_header}" "${error_contents}")
endif()
