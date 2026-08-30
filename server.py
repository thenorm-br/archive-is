#!/usr/bin/env python3
"""
====================================================================
 TELEGRAM CLOUD & FILE CATALOG - WEB APPLICATION (SAAS PORTAL)
====================================================================
Backend FastAPI com:
 - Autenticação JWT com Sistema de Aprovação de Usuários (Pendente -> Aprovado)
 - Painel Administrativo para Aprovação de Contas
 - Busca Instantânea no Banco Supabase (PostgreSQL) com Filtros
 - Exportação de Planilha CSV
 - Módulo de Varredura e Clonagem de Grupos do Telegram
====================================================================
"""

import os
import sys
import re
import csv
import json
import sqlite3
import hashlib
import urllib.request
import urllib.error
import jwt
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, Response, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# Inicialização do FastAPI
app = FastAPI(title="Acervo Digital Telegram - Web Portal", version="2.0.0")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Configurações fornecidas exclusivamente pelo ambiente de execução.
def variavel_obrigatoria(nome: str) -> str:
    valor = os.getenv(nome, "").strip()
    if not valor:
        raise RuntimeError(f"A variavel de ambiente obrigatoria {nome} nao foi configurada.")
    return valor


SUPABASE_URL = variavel_obrigatoria("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = variavel_obrigatoria("SUPABASE_KEY")
SECRET_KEY = variavel_obrigatoria("SECRET_KEY")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@acervo.com").strip().lower()
ADMIN_PASSWORD = variavel_obrigatoria("ADMIN_PASSWORD")
LOCAL_DB_PATH = DATA_DIR / "acervo_local.db"


# ===== GERENCIADOR DE BANCO DE DADOS LOCAL (USUÁRIOS & FALLBACK) =====
def inicializar_banco_local():
    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                senha_hash TEXT NOT NULL,
                role TEXT DEFAULT 'usuario',
                status TEXT DEFAULT 'pendente',
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS acervo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo TEXT NOT NULL,
                titulo_normalizado TEXT,
                formato TEXT,
                tamanho_mb REAL,
                tamanho_bytes INTEGER,
                grupo_origem TEXT,
                link_canal TEXT,
                msg_id_canal INTEGER,
                data_envio TEXT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT unico_item UNIQUE (titulo_normalizado, tamanho_bytes)
            )
        """)
        # Criar admin padrão se não houver usuários
        cursor.execute("SELECT COUNT(*) FROM usuarios")
        if cursor.fetchone()[0] == 0:
            senha_admin = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()
            cursor.execute("""
                INSERT INTO usuarios (nome, email, senha_hash, role, status)
                VALUES ('Administrador', ?, ?, 'admin', 'aprovado')
            """, (ADMIN_EMAIL, senha_admin))
        conn.commit()

inicializar_banco_local()


# ===== UTILITÁRIOS DE SEGURANÇA E SESSÃO =====
def hash_senha(senha: str) -> str:
    return hashlib.sha256(senha.encode()).hexdigest()


def normalizar_texto(texto: str) -> str:
    if not texto:
        return ""
    import unicodedata
    sem_acento = unicodedata.normalize('NFKD', texto).encode('ASCII', 'ignore').decode('ASCII')
    limpo = re.sub(r'[^a-zA-Z0-9]', ' ', sem_acento.lower())
    return ' '.join(limpo.split())


def gerar_token_sessao(usuario_id: int, email: str, role: str) -> str:
    agora = datetime.utcnow()
    return jwt.encode({
        "sub": str(usuario_id),
        "email": email,
        "role": role,
        "iat": agora,
        "exp": agora + timedelta(days=7),
    }, SECRET_KEY, algorithm="HS256")


def decodificar_token_sessao(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return {
            "id": int(payload["sub"]),
            "email": payload["email"],
            "role": payload["role"],
        }
    except Exception:
        pass
    return None


async def obter_usuario_atual(request: Request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get("session_token")
    if not token:
        return None
    return decodificar_token_sessao(token)


# ===== SERVIÇOS DO SUPABASE (BANCO CENTRAL NA NUVEM) =====
class SupabaseService:
    @staticmethod
    def buscar_arquivos(termo: str = "", formato: str = "", limit: int = 60) -> List[Dict[str, Any]]:
        endpoint = f"{SUPABASE_URL}/rest/v1/acervo?select=*&order=id.desc&limit={limit}"
        if termo:
            termo_norm = normalizar_texto(termo)
            endpoint += f"&titulo_normalizado=ilike.*{termo_norm}*"
        if formato and formato != "todos":
            endpoint += f"&formato=ilike.{formato.upper()}"

        try:
            req = urllib.request.Request(
                endpoint,
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}"
                },
                method="GET"
            )
            with urllib.request.urlopen(req, timeout=6) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
        except Exception:
            pass

        # Fallback para banco local
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = "SELECT * FROM acervo WHERE 1=1"
            params = []
            if termo:
                query += " AND titulo_normalizado LIKE ?"
                params.append(f"%{normalizar_texto(termo)}%")
            if formato and formato != "todos":
                query += " AND formato = ?"
                params.append(formato.upper())
            query += f" ORDER BY id DESC LIMIT {limit}"
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def contar_total() -> int:
        try:
            req = urllib.request.Request(
                f"{SUPABASE_URL}/rest/v1/acervo?select=id",
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Range-Unit": "items",
                    "Range": "0-0",
                    "Prefer": "count=exact"
                }
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                content_range = response.headers.get("Content-Range")
                if content_range and "/" in content_range:
                    return int(content_range.split("/")[1])
        except Exception:
            pass
        
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM acervo")
            return cursor.fetchone()[0]


# ===== ROTAS DE AUTENTICAÇÃO E PÁGINAS =====

@app.get("/login", response_class=HTMLResponse)
async def pagina_login(request: Request, erro: Optional[str] = None, sucesso: Optional[str] = None):
    usuario = await obter_usuario_atual(request)
    if usuario:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {
        "request": request,
        "erro": erro,
        "sucesso": sucesso
    })


@app.post("/auth/cadastro")
async def processar_cadastro(
    nome: str = Form(...),
    email: str = Form(...),
    senha: str = Form(...)
):
    email_limpo = email.strip().lower()
    senha_hash_val = hash_senha(senha)

    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM usuarios WHERE email = ?", (email_limpo,))
        if cursor.fetchone():
            return RedirectResponse(
                url="/login?erro=Este e-mail ja esta cadastrado!",
                status_code=status.HTTP_303_SEE_OTHER
            )
        
        cursor.execute("""
            INSERT INTO usuarios (nome, email, senha_hash, role, status)
            VALUES (?, ?, ?, 'usuario', 'pendente')
        """, (nome.strip(), email_limpo, senha_hash_val))
        conn.commit()

    return RedirectResponse(
        url="/login?sucesso=Conta criada com sucesso! Aguarde a aprovacao do administrador para acessar o portal.",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/auth/login")
async def processar_login(
    response: Response,
    email: str = Form(...),
    senha: str = Form(...)
):
    email_limpo = email.strip().lower()
    senha_hash_val = hash_senha(senha)

    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM usuarios WHERE email = ?", (email_limpo,))
        usuario = cursor.fetchone()

        if not usuario or usuario["senha_hash"] != senha_hash_val:
            return RedirectResponse(
                url="/login?erro=E-mail ou senha incorretos!",
                status_code=status.HTTP_303_SEE_OTHER
            )

        if usuario["status"] == "pendente":
            return RedirectResponse(
                url="/login?erro=Sua conta esta PENDENTE de aprovacao pelo administrador. Tente novamente mais tarde!",
                status_code=status.HTTP_303_SEE_OTHER
            )

        if usuario["status"] == "bloqueado":
            return RedirectResponse(
                url="/login?erro=Esta conta foi suspensa pelo administrador.",
                status_code=status.HTTP_303_SEE_OTHER
            )

        # Login bem-sucedido: gerar token
        token = gerar_token_sessao(usuario["id"], usuario["email"], usuario["role"])
        res = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        res.set_cookie(
            key="session_token",
            value=token,
            max_age=86400 * 7,
            httponly=True,
            secure=True,
            samesite="lax",
        )
        return res


@app.get("/logout")
async def logout():
    res = RedirectResponse(url="/login?sucesso=Voce saiu com sucesso!", status_code=status.HTTP_303_SEE_OTHER)
    res.delete_cookie("session_token")
    return res


# ===== DASHBOARD PRINCIPAL (CATÁLOGO DIGITAL) =====
@app.get("/", response_class=HTMLResponse)
async def dashboard_principal(
    request: Request,
    q: Optional[str] = "",
    formato: Optional[str] = "todos"
):
    usuario = await obter_usuario_atual(request)
    if not usuario:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    arquivos = SupabaseService.buscar_arquivos(termo=q or "", formato=formato or "todos")
    total_acervo = SupabaseService.contar_total()

    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "usuario": usuario,
        "arquivos": arquivos,
        "total_acervo": total_acervo,
        "termo_busca": q or "",
        "formato_selecionado": formato or "todos"
    })


# ===== PAINEL ADMINISTRATIVO (APROVAÇÃO DE USUÁRIOS) =====
@app.get("/admin", response_class=HTMLResponse)
async def painel_admin(request: Request):
    usuario = await obter_usuario_atual(request)
    if not usuario or usuario.get("role") != "admin":
        return RedirectResponse(url="/?erro=Acesso negado!", status_code=status.HTTP_303_SEE_OTHER)

    with sqlite3.connect(LOCAL_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM usuarios ORDER BY id DESC")
        usuarios_cadastrados = [dict(row) for row in cursor.fetchall()]

    return templates.TemplateResponse(request, "admin.html", {
        "request": request,
        "usuario": usuario,
        "usuarios": usuarios_cadastrados
    })


@app.post("/admin/usuario/{user_id}/status")
async def alterar_status_usuario(
    user_id: int,
    novo_status: str = Form(...),
    request: Request = None
):
    usuario = await obter_usuario_atual(request)
    if not usuario or usuario.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito ao administrador")

    if novo_status in ["aprovado", "pendente", "bloqueado"]:
        with sqlite3.connect(LOCAL_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE usuarios SET status = ? WHERE id = ?", (novo_status, user_id))
            conn.commit()

    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


# ===== EXPORTAÇÃO CSV =====
@app.get("/api/exportar/csv")
async def exportar_csv(request: Request):
    usuario = await obter_usuario_atual(request)
    if not usuario:
        raise HTTPException(status_code=401, detail="Nao autorizado")

    arquivos = SupabaseService.buscar_arquivos(limit=5000)
    caminho_csv = DATA_DIR / "acervo_exportado.csv"

    with open(caminho_csv, "w", newline="", encoding="utf-8-sig") as f:
        campos = ["ID", "Título", "Formato", "Tamanho (MB)", "Grupo de Origem", "Link no Telegram", "Data"]
        writer = csv.DictWriter(f, fieldnames=campos, delimiter=";")
        writer.writeheader()
        for a in arquivos:
            writer.writerow({
                "ID": a.get("id", ""),
                "Título": a.get("titulo", ""),
                "Formato": a.get("formato", ""),
                "Tamanho (MB)": f"{float(a.get('tamanho_mb', 0)):.2f}",
                "Grupo de Origem": a.get("grupo_origem", ""),
                "Link no Telegram": a.get("link_canal", ""),
                "Data": a.get("data_envio", "")
            })

    return FileResponse(
        path=caminho_csv,
        filename=f"Acervo_Telegram_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        media_type="text/csv"
    )


# ===== INICIAR SERVIDOR =====
if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*60)
    print("🚀 SERVIDOR WEB ACERVO DIGITAL INICIADO COM SUCESSO!")
    print("📌 Acesse no seu navegador: http://localhost:8000")
    print(f"🔑 Login Administrador: {ADMIN_EMAIL}")
    print("="*60 + "\n")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
