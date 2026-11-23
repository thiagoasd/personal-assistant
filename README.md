# Organizador pessoal

Gastos pessoais (Pluggy, categorias, limites), agenda com lembretes no WhatsApp e conversa com
o bot ("paguei a luz", "tomei", "adia meia hora", "me lembra sexta às 15h do dentista").
Plano, decisões e riscos em [PLANO.md](PLANO.md). **Fora do ecossistema da loja.**

## Rodar localmente

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env   # preencha o que for usar
.\.venv\Scripts\python.exe -m organizador serve
```

Painel em `http://127.0.0.1:8095`. Com `NOTIFIERS=console`, os avisos aparecem no terminal.

Demonstração com dados fictícios: `$env:DB_PATH='data/demo.db'; python -m organizador demo`.

Outros comandos: `status` (o que está configurado, sem mostrar segredos) e `sync` (lê o Pluggy agora).

## Testes

```powershell
.\.venv\Scripts\python.exe -m unittest
```

Offline: sem rede, com cliente falso no lugar do Claude.

## Produção (Easypanel)

- Docker, porta 8000, **volume em `/app/data`**.
- Variáveis em `.env.example`. `EVOLUTION_TO` é o **seu** número.
- Webhook da Evolution (instância da loja), evento `MESSAGES_UPSERT`, apontando para
  `https://<domínio>/webhook/evolution/<WEBHOOK_SECRET>`. **Antes, confira se a instância já tem
  um webhook**: a Evolution aceita um só por instância.
