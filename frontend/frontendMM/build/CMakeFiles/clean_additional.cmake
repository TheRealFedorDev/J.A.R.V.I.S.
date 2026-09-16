# Additional clean files
cmake_minimum_required(VERSION 3.16)

if("${CONFIG}" STREQUAL "" OR "${CONFIG}" STREQUAL "Debug")
  file(REMOVE_RECURSE
  "CMakeFiles/frontendMM_autogen.dir/AutogenUsed.txt"
  "CMakeFiles/frontendMM_autogen.dir/ParseCache.txt"
  "frontendMM_autogen"
  )
endif()
