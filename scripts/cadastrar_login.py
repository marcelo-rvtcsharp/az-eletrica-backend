"""
Cadastra um novo usuário/login no sistema AZ Elétrica via API.

Por segurança, só quem já tem login pode cadastrar outro usuário.
O script pede primeiro o SEU login (admin) e depois os dados do novo usuário.

Uso:
    pip install requests
    python scripts/cadastrar_login.py
"""

import getpass
import requests

BASE_URL = "https://web-production-a606c.up.railway.app"


def main():
    print("=== Cadastrar novo login no AZ Elétrica ===\n")
    print("1) Seu login (quem está cadastrando):")
    admin_email = input("   E-mail: ").strip()
    admin_senha = getpass.getpass("   Senha: ").strip()

    r = requests.post(
        f"{BASE_URL}/auth/login",
        data={"username": admin_email, "password": admin_senha},
    )
    if r.status_code != 200:
        print(f"\nNão foi possível entrar: {r.json().get('detail', r.text)}")
        return
    token = r.json()["access_token"]

    print("\n2) Dados do novo usuário:")
    nome = input("   Nome: ").strip()
    email = input("   E-mail (será o login): ").strip()
    senha = getpass.getpass("   Senha: ").strip()

    r = requests.post(
        f"{BASE_URL}/auth/registro",
        json={"nome": nome, "email": email, "senha": senha},
        headers={"Authorization": f"Bearer {token}"},
    )

    if r.status_code == 200:
        u = r.json()["usuario"]
        print(f"\nCadastrado com sucesso! {u['nome']} <{u['email']}> (ID {u['id']})")
    else:
        print(f"\nErro ({r.status_code}): {r.json().get('detail', r.text)}")


if __name__ == "__main__":
    main()
