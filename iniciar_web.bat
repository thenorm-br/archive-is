@echo off
chcp 65001 >nul
title Acervo Digital - Portal Web
color 0A

echo ===================================================
echo   ACERVO DIGITAL TELEGRAM - PORTAL WEB (SAAS)
echo ===================================================
echo.

echo Verificando dependencias da aplicacao web...
python -m pip install fastapi "uvicorn[standard]" jinja2 python-multipart requests >nul 2>&1

echo.
echo ===================================================
echo   SERVIDOR INICIADO COM SUCESSO!
echo ===================================================
echo   Acesse no seu navegador: http://localhost:8000
echo.
echo   Configure SUPABASE_URL, SUPABASE_KEY, SECRET_KEY e ADMIN_PASSWORD
echo   como variaveis de ambiente antes de iniciar.
echo ===================================================
echo.

python server.py
pause
