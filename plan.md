# Plano de Implementação: MailFlow

**Data**: 2026-11-08 | **Fonte**: [MailFlow — Documento de Requisitos e Especificações](./MailFlow%20Automa%C3%A7%C3%A3o%20de%20Envio%20de%20E-mails%20(Transacional,%20Lembretes%20e%20Relat%C3%B3rios).pdf)

## Resumo

Implementar primeiro uma API e um worker seguros para e-mail transacional e lembretes, usando Mailpit no desenvolvimento. O núcleo persistente (PostgreSQL), a fila (Redis/Celery), templates HTML/texto e idempotência precedem os recursos de campanhas promocionais, integração com provedor real e relatórios. Todos os envios passam pela abstração `EmailProvider`; fora de produção, destinatários são limitados a uma allowlist e/ou redirecionados ao Mailpit.

O documento é a única fonte de requisitos disponível no workspace. Os IDs `RF-*`, `RNF-*`, `RN-*` e `CA-*` são preservados para rastreabilidade; não foram criados IDs `REQ-*`.

## Contexto Técnico

**Linguagem/versão**: Python 3.12  
**API**: FastAPI com OpenAPI  
**Persistência**: PostgreSQL 16; entidades e migrações conforme modelo de dados da especificação  
**Fila/agendamento**: Redis, Celery e Celery Beat  
**Templates/PDF**: Jinja2; avaliar MJML para HTML responsivo; ReportLab ou WeasyPrint para PDF  
**E-mail**: Mailpit local/CI; adaptadores para Resend e Brevo isolados por `EmailProvider`  
**DNS**: dnspython para consultas SPF, DKIM e DMARC  
**Testes**: pytest, httpx, respx e integração em Docker Compose com assertions pela API do Mailpit  
**Plataforma**: Docker Compose com API, worker, scheduler/beat, PostgreSQL, Redis e Mailpit  
**Metas**: p95 evento→provedor abaixo de 30 s; suportar 10.000 envios/dia em hardware modesto; zero duplicações e zero envios a suprimidos  
**Restrições**: não enviar sem autenticação válida do domínio em staging real; segredos em variáveis de ambiente; logs sem e-mails em claro; promoção exige consentimento/base legal e descadastro imediato; cold email, multi-tenant e MTA próprio estão fora do MVP.

## Verificação de Princípios

Não há `constitution.md` no workspace. A verificação abaixo usa as restrições explícitas da especificação:

| Princípio | Verificação |
|---|---|
| Desenvolvimento seguro e reproduzível | Conforme: Mailpit por padrão, Docker Compose e allowlist/redirecionamento fora de produção. |
| Entrega confiável sem duplicidade | Conforme: persistência antes do enfileiramento, fila durável, retries e idempotência no banco. |
| Provedor substituível | Conforme: todo envio e parsing de webhook passam por `EmailProvider`. |
| Conformidade e privacidade | Conforme: consentimento, supressão, descadastro one-click, minimização e mascaramento são requisitos fundacionais. |
| Escopo do MVP | Conforme: sem prospecção fria, editor visual, MTA próprio, multi-tenant ou cobrança. |

## Diretrizes Aplicadas

Não foram encontrados arquivos de diretrizes de migração (`skills/guidelines/`) no workspace. Foram incorporadas as regras da própria especificação: sempre gerar HTML e texto simples, falhar explicitamente para variável ausente, validar assinatura de webhook, usar token de descadastro assinado, aplicar rate limit e backoff, e manter transacional separado de promocional.

## Decisões de Planejamento

- A implementação será API-first; a documentação pede uma interface mínima, mas não define telas ou fluxos de UI. Uma UI dedicada não será presumida para o MVP.
- `EmailProvider` será implementado antes dos provedores externos. Mailpit é o adaptador de desenvolvimento; Resend e Brevo serão comparados em staging antes de escolher o provedor real padrão.
- Celery + Celery Beat foi escolhido entre as opções citadas por cobrir workers e agendamento com uma integração coerente. A escolha poderá ser revista antes da primeira implementação caso surja uma restrição de execução.
- O documento cita estratégias de dados e modelos, mas não define ORM/mecanismo de migração. A escolha de SQLAlchemy/Alembic (ou alternativa equivalente) deve ser confirmada na inicialização; os contratos e entidades abaixo não dependem dessa escolha.

## Fases de Implementação

### Fase 1 — Setup e ambiente local

**Plano 1.1 — Repositório executável**  
**Requisitos**: RNF-07, RF-19  
**Insumos**: stack e ambientes (seções 3, 4 e 9.2); Sprint 0.  
**Descrição**: criar a estrutura `src/`, `tests/`, `docs/` e `docker/`; configurar Docker Compose para API, worker, beat, PostgreSQL, Redis e Mailpit. Garantir que o SMTP de desenvolvimento capture mensagens e que o modo não produtivo não consiga entregar a destinatários arbitrários.

**Plano 1.2 — Configuração, saúde e documentação inicial**  
**Requisitos**: RNF-04, RNF-06, RF-22  
**Insumos**: seções 7, 9.3, 17 e DoD.  
**Descrição**: definir configuração por ambiente, gestão de segredos por variáveis, logs estruturados e endpoints de saúde/métricas. Documentar como subir o ambiente e como verificar o e-mail capturado no Mailpit.

### Fase 2 — Núcleo transacional (US1, prioridade P1)

**Plano 2.1 — Persistência, templates e renderização**  
**Requisitos**: RF-02, RF-03, RNF-05, RNF-08, RNF-09, RNF-10; RN-02, RN-05, RN-11, RN-12  
**Insumos**: modelo de dados (seção 11), requisitos de template (12.1), segurança e privacidade (17).  
**Descrição**: modelar destinatários, templates e mensagens; suportar versões, assunto, HTML e texto simples. Declarar variáveis obrigatórias e interromper a renderização com erro claro se faltar valor. Aplicar regras para conteúdo transacional, acessibilidade, cabeçalho, anexos permitidos e mascaramento de dados pessoais.

**Plano 2.2 — API transacional e idempotência**  
**Requisitos**: RF-01, RF-07, RNF-02, RN-06, RN-07  
**Insumos**: fluxo principal (9.1), rotas (13), critérios CA-01, CA-02, CA-03 e CA-15.  
**Descrição**: implementar `POST /api/v1/messages`, validar entrada, classificação, remetente, supressão e chave de idempotência; persistir antes de enfileirar. Retornar o resultado anterior para repetição da mesma chave. Enviar somente pelo contrato `EmailProvider`.

### Fase 3 — Importação, consentimento e lembretes (US2, prioridade P1)

**Plano 3.1 — Destinatários, CSV e consentimentos**  
**Requisitos**: RF-04, RF-14; RN-01  
**Insumos**: modelo de dados (11), rotas (13), CA-04.  
**Descrição**: implementar importação CSV com normalização, validação de sintaxe/domínio, deduplicação e relatório dos rejeitados. Persistir origem, finalidade, base legal e datas de registro/revogação do consentimento.

**Plano 3.2 — Campanhas programadas e janela de envio**  
**Requisitos**: RF-05; RN-08  
**Insumos**: `campaigns`/`schedules` (11), rotas (13), CA-05.  
**Descrição**: criar campanhas e regras de lembrete com data/hora, fuso e ocorrência idempotente. Respeitar por padrão 08h–20h no fuso do destinatário, exceto transacionais urgentes. O envio promocional permanece desativado até a implementação do descadastro one-click e dos webhooks de supressão na Fase 5.

### Fase 4 — Processamento confiável (US2, prioridade P1)

**Plano 4.1 — Worker, limites e retentativas**  
**Requisitos**: RF-06, RF-08, RNF-01, RNF-02, RNF-03; RN-09  
**Insumos**: seção 12.2, CA-06 e CA-07.  
**Descrição**: processar mensagens persistidas com limites por provedor, por segundo/dia e domínio de destino. Repetir erros temporários com backoff exponencial até cinco tentativas; encaminhar falhas definitivas ou esgotadas para fila de falhas, sem perder nem duplicar mensagem após reinício.

**Plano 4.2 — Pausa, warm-up e proteção de reputação**  
**Requisitos**: RF-17, RF-20; RN-10  
**Insumos**: seções 8 e 12.3, rotas (13).  
**Descrição**: permitir pausar/retomar campanhas e cancelar pendências; limitar o volume de domínios novos de modo configurável. Pausar campanhas promocionais e notificar o operador quando os limites de bounce/reclamação forem excedidos.

### Fase 5 — Webhooks, supressão e descadastro (US3, prioridade P1)

**Plano 5.1 — Adaptadores e processamento de eventos**  
**Requisitos**: RF-09, RNF-04, RN-03  
**Insumos**: fluxo (9.1), segurança (17), CA-08, CA-09 e CA-13.  
**Descrição**: validar assinatura antes de processar webhooks; normalizar eventos de entrega, hard/soft bounce, reclamação, abertura e clique. Processar eventos de forma idempotente e registrar eventos sem confiar em payload não autenticado.

**Plano 5.2 — Supressão e one-click unsubscribe**  
**Requisitos**: RF-10, RF-11, RF-12, RF-13; RN-01, RN-02, RN-03, RN-04, RN-05  
**Insumos**: regras (8), cabeçalhos (10), CA-10 e CA-11.  
**Descrição**: bloquear globalmente hard bounce e reclamações; suprimir soft bounce após três falhas consecutivas. Implementar tokens HMAC não previsíveis, endpoint GET/POST sem login e cabeçalhos RFC 8058 em e-mails promocionais. Garantir que descadastro promocional não bloqueie e-mail genuinamente transacional.

### Fase 6 — Relatórios, domínio e operação (US4, prioridade P2)

**Plano 6.1 — Relatórios agendados e anexos**  
**Requisitos**: RF-15; RN-12  
**Insumos**: modelo/fluxos (11–13), CA-14.  
**Descrição**: gerar relatório periódico em PDF, anexar ao e-mail, validar tipos e limite padrão de 5 MB; para conteúdo sensível, preferir link com expiração.

**Plano 6.2 — Verificação de domínio e indicadores**  
**Requisitos**: RF-16, RF-18, RF-21, RF-22; RNF-06  
**Insumos**: seções 10, 13 e 16.2.  
**Descrição**: consultar SPF, DKIM e DMARC e expor pendências; disponibilizar métricas por etapa e exportação CSV de envios/consentimentos; expor `GET /health` e `GET /metrics`.

### Fase 7 — Staging real, validação e entrega

**Plano 7.1 — Provedor real e entregabilidade**  
**Requisitos**: RF-16, RNF-01, RNF-03, RNF-09, RNF-10; RN-06, RN-09, RN-10  
**Insumos**: seções 2, 3, 10, 14 e checklist 16.2.  
**Descrição**: comparar Resend e Brevo com domínio de teste autenticado; confirmar SPF/DKIM/DMARC alinhados antes de qualquer envio externo. Executar checklist de inbox/spam, cabeçalhos, unsubscribe e renderização em clientes. Não usar contas pessoais SMTP como solução.

**Plano 7.2 — Qualidade, privacidade e entrega final**  
**Requisitos**: RNF-02, RNF-04, RNF-05, RNF-06, RNF-07, RNF-08; RN-01 a RN-12  
**Insumos**: estratégia de testes (15), critérios de aceite (16), segurança (17), DoD (21).  
**Descrição**: executar testes unitários, integração, contrato, resiliência, carga e segurança; atingir cobertura mínima de 80% em templates, fila e supressão. Verificar retenção configurável, exclusão sob demanda, mascaramento e ausência de dados pessoais/segredos no repositório. Atualizar OpenAPI e documentação.

## Tarefas

### Fase 1 — Setup

- [x] T001 [Plan:1.1] Criar a estrutura inicial do projeto em `pyproject.toml`, `src/`, `tests/`, `docs/` e `docker/`.
- [x] T002 [Plan:1.1] Definir serviços, healthchecks, volumes e portas de API, worker, beat, PostgreSQL, Redis e Mailpit em `docker-compose.yml`; criar o entrypoint inicial do worker em `src/worker.py`.
- [x] T003 [Plan:1.2] Implementar leitura e validação de configuração por ambiente em `src/config.py`, incluindo modo de teste, allowlist e variáveis de segredos.
- [x] T004 [Plan:1.2] Implementar logs estruturados, endpoints `/health` e `/metrics` em `src/api/main.py` e atualizar instruções de execução em `README.md`.

### Fase 2 — Núcleo transacional (US1)

- [x] T005 [Plan:2.1] Definir entidades e migrações para destinatários, templates, mensagens, eventos e supressões em `src/db/`.
- [x] T006 [Plan:2.1] Implementar templates HTML/texto, variáveis obrigatórias e validação de renderização em `src/rendering/templates.py` e `src/templates/`.
- [x] T007 [Plan:2.1] Adicionar testes para renderização, variáveis ausentes, injeção de cabeçalho, variáveis declaradas e texto alternativo em imagens em `tests/unit/test_templates.py`.
- [x] T008 [Plan:2.2] Definir o contrato de envio e resultado comum do provedor em `src/providers/base.py`; conectar o fluxo de envio ao Mailpit via `src/providers/smtp.py`.
- [x] T009 [Plan:2.2] Implementar `POST /api/v1/messages`, persistência, validação, checagem de supressão e idempotência em `src/api/messages.py`; cobrir CA-01, CA-02 e CA-15 em `tests/integration/test_messages.py`.

### Fase 3 — Importação, consentimento e lembretes (US2)

- [x] T010 [Plan:3.1] Implementar normalização, validação e relatório de rejeições de CSV em `src/consent/import_csv.py` e `src/api/recipients.py`.
- [x] T011 [Plan:3.1] Persistir consentimentos com finalidade, base legal, origem e revogação em `src/db/models.py` e `src/api/recipients.py`; adicionar testes em `tests/integration/test_recipients_campaigns.py`.
- [x] T012 [Plan:3.2] Implementar entidades, rotas e serviço de campanha/agendamento em `src/api/campaigns.py` e `src/db/models.py`.
- [x] T013 [Plan:3.2] Implementar cálculo de ocorrências idempotentes e janela de envio por fuso em `src/scheduler/windows.py` e `src/scheduler/dispatch.py`; validar em `tests/unit/test_scheduling.py`.

### Fase 4 — Processamento confiável (US2)

- [x] T014 [Plan:4.1] Implementar tarefas Celery persistentes para buscar, renderizar e enviar mensagens em `src/queue/tasks.py` e `src/worker.py`.
- [x] T015 [Plan:4.1] Implementar limites por provedor/domínio, backoff, máximo de tentativas e fila de falhas em `src/queue/rate_limit.py` e `src/queue/retries.py`; validar falhas/reinício em `tests/integration/test_queue.py`.
- [x] T016 [Plan:4.2] Implementar transições de estado para pausar/retomar campanhas e cancelar pendências em `src/scheduler/campaign_controls.py` e `src/api/campaigns.py`.
- [x] T017 [Plan:4.2] Implementar limites configuráveis de warm-up e pausa por métricas de reputação em `src/queue/warmup.py`; validar limites em `tests/unit/test_queue_controls.py`.

### Fase 5 — Webhooks, supressão e descadastro (US3)

- [ ] T018 [Plan:5.1] Implementar parsing e validação de assinatura de webhook em `src/providers/webhook.py` e `src/api/webhooks.py`.
- [ ] T019 [Plan:5.1] Normalizar eventos e processá-los idempotentemente em `src/suppression/events.py`; cobrir payloads válidos, inválidos e duplicados em `tests/contract/test_webhooks.py`.
- [ ] T020 [Plan:5.2] Implementar regras de supressão para hard bounce, reclamação e soft bounce após três falhas em `src/suppression/service.py`.
- [ ] T021 [Plan:5.2] Implementar tokens assinados, endpoint GET/POST sem login e cabeçalhos one-click em `src/api/unsubscribe.py` e `src/providers/headers.py`; validar CA-10/CA-11 em `tests/integration/test_unsubscribe.py`.

### Fase 6 — Relatórios, domínio e operação (US4)

- [ ] T022 [Plan:6.1] Implementar geração/agendamento de PDF e validação de tamanho/tipo do anexo em `src/rendering/pdf.py` e `src/scheduler/reports.py`; cobrir CA-14 em `tests/integration/test_reports.py`.
- [ ] T023 [Plan:6.2] Implementar verificação DNS e APIs de domínio, métricas e exportação CSV em `src/domain/checks.py`, `src/api/domain.py`, `src/api/metrics.py` e `src/api/exports.py`; adicionar testes correspondentes em `tests/unit/` e `tests/integration/`.

### Fase 7 — Staging e entrega

- [ ] T024 [Plan:7.1] Implementar adaptadores Resend/Brevo em `src/providers/resend.py` e `src/providers/brevo.py`; comparar staging autenticado e registrar resultado em `docs/provider-evaluation.md`.
- [ ] T025 [Plan:7.2] Completar testes de integração, segurança, carga, retenção e cobertura; validar Compose, checklist de entregabilidade e critérios de aceite; atualizar `docs/` e OpenAPI.

## Estrutura Proposta

```text
docker/
src/
  api/          # rotas, webhooks, descadastro
  consent/
  db/           # modelos e migrações
  domain/       # verificação SPF/DKIM/DMARC
  providers/    # contrato, Mailpit/SMTP, Resend, Brevo
  queue/        # tarefas, limites, retentativas
  rendering/    # templates e PDFs
  scheduler/    # regras e fusos horários
  suppression/
  templates/    # HTML e texto simples
tests/
  unit/
  integration/
  contract/
  deliverability/
docs/
```

## Estratégia de Testes

- **Tipo de aplicação**: API e workers assíncronos; interface de operação não especificada (planejamento API-first).
- **Jornadas críticas**: envio transacional idempotente; importação e consentimento; lembrete no fuso/janela corretos; retry sem perda/duplicidade; bounce/reclamação/descadastro suprimindo o endereço; relatório PDF programado.
- **Stack primária**: pytest + httpx/respx; Docker Compose com PostgreSQL, Redis e Mailpit; verificação de mensagens pela API do Mailpit.
- **Infra**: Docker Compose como caminho primário; sem substituto especificado, portanto ausência de Docker bloqueia a suíte de integração (unitários continuam executáveis).
- **Staging externo**: provedores reais apenas com domínio de teste autenticado e destinatários allowlisted; sem credenciais, validação fica limitada ao Mailpit.
- **Dados**: destinatários sintéticos, isolados por execução; limpar dados de teste sem apagar registros de produção; fixtures gravadas para webhooks.
- **Aceite**: CA-01 a CA-15; p95 abaixo de 30 s em fila normal; zero duplicidade e zero envios a suprimidos; cobertura ≥80% em templates, fila e supressão; carga de 5.000 mensagens respeitando limites.
- **Revisão final**: confirmar assinatura inválida não altera dados, token adulterado é recusado, allowlist impede entrega real em staging, métricas e logs não expõem endereços, OpenAPI/README estão atualizados.

## Mapeamento de Requisitos

| ID fonte | Requisito resumido | Itens do plano | Evidência de implementação |
|---|---|---|---|
| RF-01 | API de envio transacional | 2.2 | `src/api/messages.py`, testes API |
| RF-02 | Templates versionados HTML/texto | 2.1 | `src/templates/`, `src/rendering/` |
| RF-03 | Validar variáveis obrigatórias | 2.1 | renderizador e testes de erro |
| RF-04 | Importação CSV validada | 3.1 | `src/api/recipients.py`, testes CSV |
| RF-05 | Agendamento com fuso | 3.2 | `src/scheduler/`, testes de horário |
| RF-06 | Fila e limite de taxa | 4.1 | `src/queue/`, testes de limite |
| RF-07 | Idempotência | 2.2, 4.1 | restrição única e testes de repetição |
| RF-08 | Retry/backoff/fila de falhas | 4.1 | worker e testes de resiliência |
| RF-09 | Webhooks autenticados | 5.1 | `src/api/webhooks.py`, testes de contrato |
| RF-10 | Lista global de supressão | 5.2 | `src/suppression/`, testes de bloqueio |
| RF-11 | Cabeçalhos/link de descadastro | 5.2 | `src/providers/`, testes de cabeçalho |
| RF-12 | Descadastro one-click | 5.2 | `src/api/unsubscribe.py`, CA-10 |
| RF-13 | Tipos transacional/promocional | 2.2, 5.2 | domínio/renderização, CA-11 |
| RF-14 | Registro de consentimento | 3.1 | `src/consent/`, testes de auditoria |
| RF-15 | Relatório PDF agendado | 6.1 | `src/rendering/pdf.py`, CA-14 |
| RF-16 | Verificação SPF/DKIM/DMARC | 6.2, 7.1 | `src/domain/`, checklist staging |
| RF-17 | Aquecimento gradual | 4.2 | política de warm-up e testes |
| RF-18 | Painel/métricas de entrega | 6.2 | métricas e consultas de agregação |
| RF-19 | Modo de teste seguro | 1.1 | configuração de allowlist/redirecionamento |
| RF-20 | Pausar/retomar/cancelar campanhas | 4.2 | rotas e testes de estado |
| RF-21 | Exportação CSV | 6.2 | exportadores e testes de conteúdo |
| RF-22 | Health e métricas | 1.2, 6.2 | endpoints `/health` e `/metrics` |
| RNF-01 | p95 evento→provedor <30 s | 4.1, 7.1 | teste de desempenho e medição |
| RNF-02 | Durabilidade e entrega sem duplicidade | 2.2, 4.1, 7.2 | reinício de worker e testes de idempotência |
| RNF-03 | Escala horizontal / 10.000 por dia | 4.1, 7.1 | teste de carga/configuração dos workers |
| RNF-04 | Segredos, assinatura e tokens seguros | 1.2, 5.1, 5.2, 7.2 | testes de segurança/configuração |
| RNF-05 | LGPD, retenção, exclusão e mascaramento | 2.1, 7.2 | testes de privacidade/retention |
| RNF-06 | Logs estruturados e métricas por etapa | 1.2, 6.2 | logs e métricas correlacionados |
| RNF-07 | Compose sobe todos os serviços | 1.1, 7.2 | smoke test do Compose |
| RNF-08 | Testes e cobertura ≥80% nos módulos críticos | 7.2 | relatório de cobertura |
| RNF-09 | Compatibilidade de clientes de e-mail | 2.1, 7.1 | checklist Gmail/Outlook/Apple/celular |
| RNF-10 | Acessibilidade e texto simples | 2.1, 7.1 | snapshots e revisão visual |
| RN-01 | Promoção exige consentimento/base compatível | 3.1, 5.2 | testes de elegibilidade |
| RN-02 | Conteúdo transacional sem promoção | 2.1, 5.2 | validação e testes de classificação |
| RN-03 | Supressão imediata e soft bounce após 3 falhas | 5.1, 5.2 | testes de eventos e contador |
| RN-04 | Descadastro imediato, sem login | 5.2 | testes GET/POST e latência |
| RN-05 | Identificação da empresa em promocionais | 2.1, 5.2 | validação de template |
| RN-06 | From alinhado com domínio autenticado | 2.2, 7.1 | teste de configuração de domínio |
| RN-07 | Unicidade campanha/destinatário/ocorrência | 2.2, 3.2 | índice único e teste de repetição |
| RN-08 | Janela 08h–20h por fuso | 3.2 | testes de horário e fuso |
| RN-09 | Respeito aos limites do provedor | 4.1 | testes de rate limit |
| RN-10 | Pausa automática por reputação | 4.2, 5.1 | teste de limiar e notificação |
| RN-11 | Cobrança respeitosa | 2.1 | diretrizes e revisão de templates |
| RN-12 | Limite/tipos de anexos e links expiráveis | 2.1, 6.1 | validação de anexos |
