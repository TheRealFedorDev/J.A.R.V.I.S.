# Additional clean files
cmake_minimum_required(VERSION 3.16)

if("${CONFIG}" STREQUAL "" OR "${CONFIG}" STREQUAL "Debug")
  file(REMOVE_RECURSE
  "CMakeFiles/JarvisUI_autogen.dir/AutogenUsed.txt"
  "CMakeFiles/JarvisUI_autogen.dir/ParseCache.txt"
  "JarvisUI_autogen"
  )
endif()
