add_compile_options(-std=c++14)

set(OUTPUT_DIR "${CMAKE_CURRENT_BINARY_DIR}")

include_directories("${PROJECT_SOURCE_DIR}")

add_library(SESSIOND_TEST_LIB
  ProtobufCreators.cpp
  ProtobufCreators.h
  SessiondMocks.h
  )

target_link_libraries(SESSIOND_TEST_LIB SESSION_MANAGER gmock_main pthread rt)

foreach(session_test session_credit local_enforcer cloud_reporter async_service
        session_manager_handler sessiond_integ session_state session_rules
        credit_pool session_store store_client)
  add_executable(${session_test}_test test_${session_test}.cpp)
  target_link_libraries(${session_test}_test SESSIOND_TEST_LIB)
  add_test(test_${session_test} ${session_test}_test)
endforeach(session_test)
