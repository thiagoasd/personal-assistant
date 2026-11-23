# Organizador pessoal: plano

Projeto **pessoal**, separado da loja. Mesma forma dos outros sistemas (Python 3.12, Flask,
SQLite em WAL, waitress, um worker só, Docker no Easypanel), mas com banco, conta Pluggy e
painel próprios. Não lê nem escreve nada dos sistemas do Empório.

## O que ele faz

1. **Gastos:** lê suas contas pela conta Pluggy nova (só leitura), aplica categorias por
   regras ("contém IFOOD → Restaurantes"), deixa você revisar o que ficou sem categoria e mostra
   quanto foi para cada categoria no mês, com limite mensal opcional por categoria.
2. **Agenda e lembretes:** você cadastra contas a pagar (com valor), remédios, compromissos e
   tarefas, com repetição (a cada N horas, dias, semanas, meses ou anos).
3. **WhatsApp:** o bot avisa no horário e insiste até você responder (cada tipo tem seu ritmo).
4. **Conversa:** você responde em texto livre ("paguei a luz", "tomei", "adia meia hora",
   "me lembra sexta às 15h do dentista", "quanto gastei com mercado?") e o Claude transforma
   isso em ações no banco local.

## Decisões

- **Projeto novo** (`organizador-pessoal`), separado do Livro-caixa: dinheiro pessoal fora do
  sistema da empresa.
- **Conta Pluggy nova**, com `PLUGGY_CLIENT_ID` e `PLUGGY_CLIENT_SECRET` próprios. Só leitura:
  nunca inicia pagamento nem transferência.
- **WhatsApp pela mesma instância Evolution da loja**, conversando no privado com o número da
  loja. O bot só aceita mensagem **privada** vinda do seu número (`EVOLUTION_TO`). Mensagem de
  qualquer outra pessoa ou de grupo é descartada **sem ser gravada**.
- **Conversa com Claude** (`claude-haiku-5-5` por padrão, configurável em `CLAUDE_MODEL`). A IA
  só tem ferramentas sobre o banco local: listar, marcar feito, adiar, pular, criar lembrete,
  desativar lembrete (reversível no painel) e resumir gastos. Não apaga nada e não mexe em
  dinheiro. Sem `ANTHROPIC_API_KEY`, o bot entende comandos simples ("feito", "feito #12",
  "adiar 1h", "pendentes", "agenda").
- **Contagem dupla no cartão:** a compra entra pelo cartão; o pagamento da fatura na conta é
  da categoria "Pagamento de fatura", que **não entra** nos totais. O mesmo vale para
  "Transferência entre contas".

## Como funcionam os lembretes

- Cada item gera uma **ocorrência** quando chega a hora do aviso (horário menos a antecedência).
  Depois disso o item avança para a próxima repetição.
- A ocorrência fica **pendente** até você marcar feito, pular ou adiar.
- O primeiro aviso sai sempre no horário. As **insistências** respeitam o silêncio da noite
  (22h às 7h, configurável) e param depois do número máximo de avisos.
- Ritmo padrão por tipo (dá para mudar por item):

  | Tipo | Antecedência | Insiste a cada | Máximo de avisos |
  |---|---|---|---|
  | Conta a pagar | 1 dia | 6 h | 6 |
  | Remédio | na hora | 30 min | 4 |
  | Compromisso | 1 h | não insiste | 1 |
  | Tarefa | na hora | 4 h | 3 |

- **Menu numerado:** cada aviso termina com "Responda: 1 Paguei · 2 Adiar 1 h · 3 Pular" (o texto
  muda por tipo; compromisso adia 15 min). Botões de verdade só funcionam na API oficial da Meta;
  pela Evolution (WhatsApp Web) eles não são confiáveis, por isso o menu é texto.
  - "1", "2" ou "3" vale para o último aviso enviado, sem passar pela IA.
  - Se outro aviso pendente saiu até 30 min antes, o bot não adivinha: pede para responder em
    cima do aviso ou mandar "1 #12".
  - Respondendo **em cima** (citando) de um aviso, o bot lê o `#12` da mensagem citada.
- Se o servidor ficou fora do ar e várias repetições passaram, só a mais recente vira aviso
  (não chega uma enxurrada de "tome o remédio").

## Fases

- **Fase 1 (este esqueleto):** banco, agenda com repetição, lembretes no WhatsApp, webhook da
  Evolution, conversa com Claude e comandos simples, Pluggy, categorias, regras, resumo do mês
  e painel com login. Testes offline.
- **Fase 2:** ligar conta a pagar com o lançamento do banco (marcar "paga" sozinha quando o
  débito aparecer, sempre pedindo sua confirmação no WhatsApp); resumo semanal de gastos;
  aviso quando uma categoria passar do limite.
- **Fase 3:** categorizar pelo WhatsApp ("isso foi mercado"), sugestões de regra pela IA,
  gráficos de evolução mês a mês.

## Pendências e riscos

- **Webhook na instância da loja:** a Evolution aceita **um webhook por instância**. Antes de
  configurar, confira no painel da Evolution se a instância da loja já tem um webhook; se tiver,
  o nosso o substituiria. Se houver conflito, a saída é uma instância nova só para o organizador.
- O número da loja recebe mensagens de clientes; o organizador recebe todas pelo webhook, mas
  descarta sem gravar as que não forem suas.
- Aviso aceito pela Evolution não prova que chegou no celular.
- A conta Pluggy nova ainda precisa ser criada, e as conexões dos bancos adicionadas no painel.

## Configuração (nomes; valores só no Easypanel ou no `.env`)

| Variável | Para quê |
|---|---|
| `DB_PATH` | banco SQLite (`/app/data/organizador.db` no Docker; volume obrigatório) |
| `APP_PASSWORD`, `SECRET_KEY`, `SECURE_COOKIES` | login do painel (sem senha, só sobe em 127.0.0.1) |
| `NOTIFIERS` | `console` (local) ou `evolution` |
| `EVOLUTION_URL`, `EVOLUTION_APIKEY`, `EVOLUTION_INSTANCE` | instância da loja |
| `EVOLUTION_TO` | **seu** número (destino dos avisos e único remetente aceito) |
| `WEBHOOK_SECRET` | segredo no caminho do webhook (`/webhook/evolution/<segredo>`) |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` | conversa com Claude |
| `PLUGGY_CLIENT_ID`, `PLUGGY_CLIENT_SECRET` | conta Pluggy nova |
| `QUIET_START`, `QUIET_END` | silêncio das insistências (padrão 22 e 7) |
