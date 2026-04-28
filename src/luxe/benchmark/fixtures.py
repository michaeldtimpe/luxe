"""Synthetic test repositories with known issues for deterministic benchmarking.

Each fixture creates a self-contained repo with planted bugs, missing docs,
or feature gaps so benchmark results are comparable across model configs.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def _init_git(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit", "--allow-empty"],
        cwd=path, capture_output=True,
        env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com",
             "HOME": str(path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add project files"],
        cwd=path, capture_output=True,
        env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t.com",
             "HOME": str(path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )


def create_python_api(base: Path) -> Path:
    """Small Python API with planted bugs — ~200 LOC, 6 files.

    Known issues (ground truth for review benchmarks):
    1. SQL injection in db.py:get_user (string formatting, not parameterized)
    2. Missing input validation in api.py:create_user (no email/name checks)
    3. Hardcoded secret in config.py:SECRET_KEY
    4. Race condition in cache.py:get_or_set (check-then-act without lock)
    5. Unclosed file handle in utils.py:read_config
    6. Missing error handling in api.py:delete_user (bare except)
    """
    repo = base / "python-api"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "tests").mkdir(exist_ok=True)

    (repo / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "demo-api"
        version = "0.1.0"
        requires-python = ">=3.11"
        dependencies = ["flask>=3.0", "sqlite3"]
    """))

    (repo / "src" / "__init__.py").write_text("")

    (repo / "src" / "config.py").write_text(textwrap.dedent("""\
        import os

        DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///app.db")
        SECRET_KEY = "super-secret-key-do-not-share-12345"
        DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
        MAX_RETRIES = 3
        CACHE_TTL = 300
    """))

    (repo / "src" / "db.py").write_text(textwrap.dedent("""\
        import sqlite3
        from src.config import DATABASE_URL


        def get_connection():
            return sqlite3.connect(DATABASE_URL.replace("sqlite:///", ""))


        def get_user(user_id):
            conn = get_connection()
            cursor = conn.cursor()
            query = f"SELECT * FROM users WHERE id = '{user_id}'"
            cursor.execute(query)
            row = cursor.fetchone()
            conn.close()
            return row


        def create_user_record(name, email):
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO users (name, email) VALUES (?, ?)",
                (name, email),
            )
            conn.commit()
            conn.close()
            return cursor.lastrowid


        def delete_user_record(user_id):
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
            conn.close()
    """))

    (repo / "src" / "api.py").write_text(textwrap.dedent("""\
        from src.db import get_user, create_user_record, delete_user_record


        def get_user_handler(user_id):
            user = get_user(user_id)
            if user is None:
                return {"error": "User not found"}, 404
            return {"id": user[0], "name": user[1], "email": user[2]}, 200


        def create_user(data):
            name = data.get("name", "")
            email = data.get("email", "")
            uid = create_user_record(name, email)
            return {"id": uid, "name": name, "email": email}, 201


        def delete_user(user_id):
            try:
                delete_user_record(user_id)
                return {"status": "deleted"}, 200
            except:
                return {"error": "something went wrong"}, 500
    """))

    (repo / "src" / "cache.py").write_text(textwrap.dedent("""\
        import time

        _cache = {}


        def get_or_set(key, factory, ttl=300):
            if key in _cache:
                value, expires = _cache[key]
                if time.time() < expires:
                    return value

            value = factory()
            _cache[key] = (value, time.time() + ttl)
            return value


        def invalidate(key):
            _cache.pop(key, None)


        def clear():
            _cache.clear()
    """))

    (repo / "src" / "utils.py").write_text(textwrap.dedent("""\
        import json


        def read_config(path):
            f = open(path)
            data = json.load(f)
            return data


        def sanitize_input(text):
            return text.strip()[:255]


        def format_response(data, status=200):
            return {"data": data, "status": status}
    """))

    (repo / "tests" / "__init__.py").write_text("")
    (repo / "tests" / "test_api.py").write_text(textwrap.dedent("""\
        def test_create_user():
            # TODO: implement
            pass

        def test_get_user():
            # TODO: implement
            pass
    """))

    (repo / "README.md").write_text(textwrap.dedent("""\
        # Demo API

        A simple user management API for testing.

        ## Setup
        ```
        pip install -e .
        ```
    """))

    _init_git(repo)
    return repo


def create_js_webapp(base: Path) -> Path:
    """Small JS web app with planted issues — ~250 LOC, 8 files.

    Known issues:
    1. XSS vulnerability in render.js:renderComment (innerHTML without escaping)
    2. Missing CSRF protection in handlers.js:handleSubmit
    3. Prototype pollution in utils.js:deepMerge
    4. Insecure default in config.js:CORS_ORIGIN set to "*"
    5. Memory leak in store.js:subscribe (listeners never cleaned up)
    6. Missing null check in api.js:fetchUser (crashes on network error)
    7. Unused dependency in package.json (lodash imported but never used)
    """
    repo = base / "js-webapp"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "src").mkdir(exist_ok=True)

    (repo / "package.json").write_text(textwrap.dedent("""\
        {
          "name": "demo-webapp",
          "version": "0.1.0",
          "type": "module",
          "dependencies": {
            "express": "^4.18.0",
            "lodash": "^4.17.21"
          }
        }
    """))

    (repo / "src" / "config.js").write_text(textwrap.dedent("""\
        export const API_URL = process.env.API_URL || "http://localhost:3000";
        export const CORS_ORIGIN = "*";
        export const SESSION_SECRET = "keyboard-cat";
        export const MAX_UPLOAD_SIZE = 10 * 1024 * 1024;
    """))

    (repo / "src" / "api.js").write_text(textwrap.dedent("""\
        import { API_URL } from "./config.js";

        export async function fetchUser(userId) {
            const response = await fetch(`${API_URL}/users/${userId}`);
            const data = response.json();
            return data;
        }

        export async function createUser(name, email) {
            const response = await fetch(`${API_URL}/users`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name, email }),
            });
            if (!response.ok) {
                throw new Error(`Failed to create user: ${response.status}`);
            }
            return response.json();
        }

        export async function deleteUser(userId) {
            return fetch(`${API_URL}/users/${userId}`, { method: "DELETE" });
        }
    """))

    (repo / "src" / "render.js").write_text(textwrap.dedent("""\
        export function renderComment(container, comment) {
            const div = document.createElement("div");
            div.innerHTML = `<p class="comment">${comment.text}</p>
                             <span class="author">${comment.author}</span>`;
            container.appendChild(div);
        }

        export function renderUserList(container, users) {
            container.innerHTML = "";
            for (const user of users) {
                const li = document.createElement("li");
                li.textContent = `${user.name} (${user.email})`;
                container.appendChild(li);
            }
        }
    """))

    (repo / "src" / "handlers.js").write_text(textwrap.dedent("""\
        import { createUser, deleteUser } from "./api.js";

        export async function handleSubmit(formData) {
            const name = formData.get("name");
            const email = formData.get("email");
            const result = await createUser(name, email);
            return result;
        }

        export async function handleDelete(userId) {
            if (!confirm("Are you sure?")) return;
            await deleteUser(userId);
            window.location.reload();
        }
    """))

    (repo / "src" / "utils.js").write_text(textwrap.dedent("""\
        export function deepMerge(target, source) {
            for (const key of Object.keys(source)) {
                if (source[key] instanceof Object && key in target) {
                    Object.assign(source[key], deepMerge(target[key], source[key]));
                }
            }
            Object.assign(target, source);
            return target;
        }

        export function debounce(fn, ms) {
            let timer;
            return (...args) => {
                clearTimeout(timer);
                timer = setTimeout(() => fn(...args), ms);
            };
        }
    """))

    (repo / "src" / "store.js").write_text(textwrap.dedent("""\
        const listeners = [];
        let state = {};

        export function getState() {
            return { ...state };
        }

        export function setState(partial) {
            state = { ...state, ...partial };
            for (const fn of listeners) {
                fn(state);
            }
        }

        export function subscribe(fn) {
            listeners.push(fn);
        }
    """))

    (repo / "README.md").write_text(textwrap.dedent("""\
        # Demo Web App

        A simple user management frontend for testing.

        ## Setup
        ```
        npm install
        npm start
        ```
    """))

    _init_git(repo)
    return repo


def create_mixed_repo(base: Path) -> Path:
    """Mixed Python + JS repo — ~150 LOC, for summarization/docs benchmarks.

    No planted bugs — designed to test whether agents can accurately
    describe structure, identify entry points, and document public APIs.
    """
    repo = base / "mixed-repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "backend").mkdir(exist_ok=True)
    (repo / "frontend").mkdir(exist_ok=True)

    (repo / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "fullstack-demo"
        version = "0.1.0"
    """))

    (repo / "backend" / "__init__.py").write_text("")
    (repo / "backend" / "app.py").write_text(textwrap.dedent("""\
        from backend.models import User
        from backend.auth import require_auth


        def create_app():
            users = {}
            next_id = 1

            def handle_create(data):
                nonlocal next_id
                user = User(id=next_id, name=data["name"], email=data["email"])
                users[next_id] = user
                next_id += 1
                return user.to_dict()

            @require_auth
            def handle_list():
                return [u.to_dict() for u in users.values()]

            return {"create": handle_create, "list": handle_list}
    """))

    (repo / "backend" / "models.py").write_text(textwrap.dedent("""\
        from dataclasses import dataclass


        @dataclass
        class User:
            id: int
            name: str
            email: str

            def to_dict(self):
                return {"id": self.id, "name": self.name, "email": self.email}
    """))

    (repo / "backend" / "auth.py").write_text(textwrap.dedent("""\
        import functools


        def require_auth(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                # TODO: implement real auth check
                return fn(*args, **kwargs)
            return wrapper
    """))

    (repo / "frontend" / "index.html").write_text(textwrap.dedent("""\
        <!DOCTYPE html>
        <html>
        <head><title>Fullstack Demo</title></head>
        <body>
            <div id="app"></div>
            <script src="main.js"></script>
        </body>
        </html>
    """))

    (repo / "frontend" / "main.js").write_text(textwrap.dedent("""\
        async function loadUsers() {
            const resp = await fetch("/api/users");
            const users = await resp.json();
            const app = document.getElementById("app");
            app.innerHTML = users.map(u => `<div>${u.name} — ${u.email}</div>`).join("");
        }

        document.addEventListener("DOMContentLoaded", loadUsers);
    """))

    (repo / "README.md").write_text("# Fullstack Demo\\n\\nA minimal fullstack app.\\n")

    _init_git(repo)
    return repo


FIXTURES = {
    "python-api": create_python_api,
    "js-webapp": create_js_webapp,
    "mixed-repo": create_mixed_repo,
}
