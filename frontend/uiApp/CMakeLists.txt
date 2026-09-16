cmake_minimum_required(VERSION 3.16)

project(JarvisUI VERSION 1.0.0 LANGUAGES CXX)

# Включаем C++17 (требуется для Qt 6)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Автоматический запуск инструментов Qt (MOC, UIC, RCC)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTOUIC ON)
set(CMAKE_AUTORCC ON)

# Поиск компонентов Qt 6
find_package(Qt6 REQUIRED COMPONENTS Widgets)

# Список исходных файлов
set(SOURCES
    uiApp/src/main.cpp
)

# Создание исполняемого файла (убрано дублирование main.cpp)
add_executable(${PROJECT_NAME} ${SOURCES})

# Линковка библиотек Qt
target_link_libraries(${PROJECT_NAME} PRIVATE Qt6::Widgets)
