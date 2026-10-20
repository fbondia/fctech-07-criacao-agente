# Assistente do Residencial Aurora

API Python com FastAPI, Gemini e **Google ADK 2.9.1**, gerenciada por uv.
O [enunciado original](docs/enunciado.md) foi preservado para consulta.

## Arquitetura

O modelo interpreta o pedido e escolhe o especialista. As tools e as transações
SQLite aplicam as regras. A aplicação não interpreta intenções com expressões
regulares nem usa respostas fixas para substituir o Gemini.

| Agente em `aurora/agents.py` | Responsabilidade | Tools |
| --- | --- | --- |
| `aurora` | Atendimento principal e distribuição de assuntos | Transferência nativa `transfer_to_agent` |
| `reservas` | Áreas, disponibilidade, reservas próprias, criação e cancelamento | `listar_areas`, `consultar_disponibilidade`, `minhas_reservas`, `reservar_area`, `cancelar_reserva` |
| `portaria` | Consulta e autorização de visitantes próprios | `meus_visitantes`, `autorizar_visitante` |
| `regulamento` | Responder dúvidas com evidência recuperada do arquivo | `consultar_regulamento` |

Os especialistas são `LlmAgent` reais registrados em `sub_agents`. Recebem
transferências do principal e podem devolver o controle a ele quando muda o assunto.
Transferências diretas entre especialistas são desabilitadas. Essa topologia mantém
as responsabilidades pequenas e deixa o agente solicitante identificável no histórico.
O principal não tem tools de dados nem o conteúdo do regulamento nas instruções.

`aurora/runtime.py` monta `App` com `ResumabilityConfig(is_resumable=True)` e `Runner`.
O `SqliteSessionService` do ADK grava sessões, estado e eventos completos em
`.runtime/sessoes.sqlite3`. `aurora/storage.py` guarda referências, identidade imutável,
reservas, visitantes e confirmações em `.runtime/condominio.sqlite3`. Os eventos são
retornados diretamente pelo serviço ADK, em ordem, sem reconstrução ou remoção de campos.
Não há compactação do histórico.

As confirmações usam `ToolContext.request_confirmation`. O runtime encontra o evento
`adk_request_confirmation` e indexa seu ID, chamada original, agente e invocation.
A rota de confirmação devolve ao Runner um `FunctionResponse` com esse ID e a mesma
invocation. O ADK reexecuta a tool no especialista solicitante. Além da confirmação
ADK, a tool exige um contexto de aprovação interno criado exclusivamente pela rota
HTTP e valida os detalhes contra a pendência persistida. O modelo não recebe esse
contexto como argumento. As respostas `confirmado` só aceitam booleanos JSON.

O servidor usa um único worker e um lock por sessão. Sessões diferentes podem ser
atendidas simultaneamente. Um lock de arquivo impede dois processos de API usando o
mesmo diretório e impede a restauração enquanto o servidor está ativo. Não é
necessário subir banco, container ou serviço adicional.

## Garantias

### 1. Cobrança e acesso exigem confirmação sistêmica

- `aurora/tools.py`, `gated`: toda reserva com taxa positiva e toda autorização
  chamam `request_confirmation`, sem gravar o efeito. Uma confirmação declarada em
  texto, um argumento extra ou mesmo `tool_confirmation` sem o contexto interno
  da rota não concede aprovação. Taxas vêm da referência local de áreas.
- `aurora/runtime.py`, `confirm`: exige pendência da mesma sessão, responde com
  `FunctionResponse(name="adk_request_confirmation")` e retoma a invocation original.
  IDs desconhecidos, de outra sessão ou já respondidos retornam `409`.
- `aurora/storage.py`, `resolve`: valida sessão, chamada, ação e detalhes; executa
  o efeito e marca a confirmação como respondida na **mesma transação**. Negar só
  encerra a pendência. Reenvio não repete o efeito.
- `reservar_area`/`reserve_free` e `cancelar_reserva`/`cancel` executam reserva
  gratuita e cancelamento próprio sem confirmação. Não existe pagamento real:
  a taxa registrada nos detalhes determina a necessidade de aprovação.

Se a geração de texto falhar depois de o efeito ser confirmado, a API devolve o
resultado persistido. Se a retomada não executar a tool, devolve `503` e mantém a
pendência, sem alegar sucesso. Enquanto há pendência, novas mensagens retornam
`200` com a mesma lista e pedem resposta pela rota de confirmações.

### 2. A sessão pertence a um único apartamento

- `aurora/storage.py`, tabela `identidades` e trigger `identidade_imutavel`:
  o apartamento é registrado uma vez e não pode ser atualizado.
- `aurora/tools.py`, tools de dados: resolvem `store.identity(tool_context.session.id)`.
  Nenhuma aceita apartamento ou session ID como parâmetro do modelo. O estado
  `apartamento` da sessão ADK é informativo; a referência autorizadora é a tabela
  imutável, fora do estado editável dos agentes.
- `Store.cancel` filtra o `UPDATE` por apartamento, área e data. As consultas de
  reservas e visitantes também filtram pelo apartamento autenticado.
- `Store.available` lê somente a existência de reserva. Conflitos retornam
  `ocupada`, sem código, nome ou apartamento de terceiros.

As rotas `/apartamentos/...` são as rotas administrativas de verificação exigidas
pelo desafio e deliberadamente não têm autenticação. Não são tools dos agentes.

### 3. Conversas e dados sobrevivem ao reinício

- `aurora/runtime.py`, `session`/`events`: lê a sessão e todos os eventos completos
  do `SqliteSessionService`, incluindo chamadas e respostas de tools.
- `aurora/storage.py`, `Store`: inicializa os dados somente em banco novo. Reabrir
  a aplicação não importa os seeds novamente nem remove cancelamentos.
- `Runtime.session` reindexa confirmações a partir dos eventos persistidos, sem
  reabrir pendências já respondidas. Aprovações continuam funcionando após reiniciar.
- `reservas.codigo` é chave primária, gerada pelo sistema com UUID. Cancelar apenas
  marca `ativa=0`, mantendo o código ocupado até uma restauração explícita.

`aurora-reset` inicia um novo ambiente: restaura os JSONs e **apaga também todas as
sessões, eventos, identidades e confirmações**, com a API parada. Nunca edita `dados/`.

### 4. O regulamento é consultado seletivamente

- `aurora/regulation.py`, `Regulation.search`: segmenta o Markdown por capítulo e
  artigo, classifica pela consulta e retorna no máximo dois artigos de um único
  capítulo pertinente, com a referência de origem.
- `aurora/agents.py`, agente `regulamento`: usa a tool de busca para fundamentar a
  resposta. O principal contém só instruções de atendimento e transferência.
- Consulta sem assunto específico pede esclarecimento. A pergunta sobre piscina
  aos domingos recupera o art. 22, cujo fechamento é **20h**, sem trazer capítulos
  sobre garagem, animais ou outros temas.

A busca é lexical e local, sem embeddings nem uma segunda chamada externa.
Artigos pertinentes ficam nos eventos por serem evidência da tool; o arquivo
inteiro fica apenas no retriever. Sinônimos pouco relacionados ao texto podem exigir
uma reformulação da consulta pelo especialista.

### 5. Duas aprovações concorrentes não duplicam a reserva

- `aurora/storage.py`, índice `reserva_exclusiva`: `UNIQUE(area,data) WHERE ativa=1`
  garante exclusividade **no INSERT**, mesmo em conexões independentes.
- `Store.connection(write=True)` usa `BEGIN IMMEDIATE`, timeout de 30s e rollback
  em falha. Nenhuma transação mantém o banco bloqueado durante uma chamada Gemini.
- `Store._reserve` converte conflito de integridade em resultado normal `ocupada`.
  `Store.resolve` consome a confirmação também no caso de a outra reserva vencer.
  Assim, as duas aprovações respondem `200` e apenas uma cria a reserva.

Os testes em `tests/test_api.py` verificam HTTP real, negação, reenvio, isolamento,
eventos, reinício de processo com pendência e corrida entre duas aprovações. Há ainda
testes de transação em threads independentes, contexto forjado e hashes dos seeds.
A [matriz de validação](docs/validacao.md) distingue essa verificação da validação
de linguagem natural com Gemini.

## Como rodar

### Pré-requisitos e configuração

Python **3.12 ou superior** (até 3.14), [uv](https://docs.astral.sh/uv/), Linux ou macOS
e uma chave do [Google AI Studio](https://aistudio.google.com/apikey).

Na pasta do desafio:

```bash
cp .env.example .env
uv sync --locked
```

Edite `.env` e preencha `GOOGLE_API_KEY`. O arquivo de exemplo contém somente os
nomes das variáveis, com valores vazios:

| Variável | Uso |
| --- | --- |
| `GOOGLE_API_KEY` | Obrigatória para conversar com Gemini; nunca é versionada |
| `GEMINI_MODEL` | Opcional; vazio usa `gemini-2.5-flash` em todos os agentes |
| `AURORA_DATA_DIR` | Opcional; vazio usa `.runtime/` na raiz do projeto |

O modelo padrão consta na [documentação oficial de modelos Gemini](https://ai.google.dev/gemini-api/docs/models).
Verifique no seu AI Studio a disponibilidade do modelo e a cota ativa; configure
`GEMINI_MODEL` se necessário. O fluxo completo requer algumas dezenas de chamadas.
Falhas de chave, modelo ou cota retornam `503` nas conversas. Criar sessão e usar as
rotas de verificação não chama o modelo.

O ADK está fixado em `google-adk==2.9.1`, e `uv.lock` fixa a resolução transitiva.
APIs consultadas: [confirmação de ações](https://adk.dev/tools-custom/confirmation/)
e os arquivos `runners.py`, `agents/_agent_router.py`,
`flows/llm_flows/request_confirmation.py`, `agents/context.py` e
`sessions/sqlite_session_service.py` da distribuição instalada **2.9.1**.
A confirmação é experimental no ADK. Foi testada com seu `SqliteSessionService`,
App resumível e a topologia descrita, inclusive após reiniciar o processo.

### Restaurar e iniciar

```bash
uv run aurora-reset
uv run aurora-api
```

A API responde em `http://localhost:8000`. O OpenAPI interativo está em
`http://localhost:8000/docs`. Para continuar uma conversa após Ctrl+C, basta executar
`uv run aurora-api` novamente, sem rodar o reset. Use somente um worker.

### Experimentar a API

```bash
curl http://localhost:8000/apartamentos/101/reservas
curl http://localhost:8000/apartamentos/302/visitantes

curl -X POST http://localhost:8000/sessoes \
  -H 'Content-Type: application/json' \
  -d '{"apartamento":"101"}'
```

Copie o `session_id` retornado e use-o no lugar de `SESSAO`:

```bash
curl -X POST http://localhost:8000/sessoes/SESSAO/mensagens \
  -H 'Content-Type: application/json' \
  -d '{"texto":"Reserve o salão de festas para 2030-04-20."}'

curl -X POST http://localhost:8000/sessoes/SESSAO/confirmacoes \
  -H 'Content-Type: application/json' \
  -d '{"id":"CONFIRMACAO","confirmado":true}'

curl http://localhost:8000/sessoes/SESSAO/eventos
```

`CONFIRMACAO` é o `id` devolvido em `confirmacoes_pendentes`. Use `false` para negar.
As respostas de conversa sempre têm `resposta` e `confirmacoes_pendentes`. Sessão
inexistente retorna `404`; confirmação inexistente, de outra sessão ou consumida
retorna `409`. Apartamento inexistente na criação retorna `404`; suas rotas de
verificação retornam listas vazias. Corpos inválidos retornam `422`.

### Verificar a implementação

```bash
uv run pytest -q
uv run ruff check aurora tests
uv run ruff format --check aurora tests
git diff --exit-code -- dados/
```

Os testes usam diretórios temporários e uma porta local livre; não precisam de chave
nem alteram os dados do seu ambiente. Somente o modelo é substituído por um fixture
determinístico de testes. A API normal sempre usa Gemini. Valide também o fluxo do
enunciado em linguagem natural com sua chave e cota antes de entregar.
