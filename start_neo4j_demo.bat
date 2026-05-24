@echo off
setlocal
set "JAVA_HOME=C:\Program Files\Eclipse Adoptium\jdk-21.0.10.7-hotspot"
set "NEO4J_HOME=C:\Users\css\tools\neo4j-community-5.26.23"
set "PATH=%JAVA_HOME%\bin;%PATH%"
cd /d "%NEO4J_HOME%"
call "%NEO4J_HOME%\bin\neo4j.bat" console
