# Decisões de Planejamento — MailFlow

**Data**: 2026-11-08  
**Fonte**: documento de requisitos do MailFlow disponível no workspace.

## 1. Provedor para staging

- **Decisão de planejamento**: manter Resend e Brevo atrás de `EmailProvider` e comparar ambos em staging; não fixar o provedor padrão antes de validar limites vigentes, webhooks e entregabilidade.
- **Racional**: a especificação recomenda testar ambos e destaca que limites e planos gratuitos mudam. A arquitetura abstrata evita acoplamento e não bloqueia o MVP local.
- **Alternativas consideradas**: escolher Resend desde o início; escolher Brevo desde o início; SES como alternativa paga de escala. A primeira decisão de produção deve ser tomada após os experimentos e a conferência das páginas oficiais.

## 2. Fila e agendador

- **Decisão**: usar Redis + Celery + Celery Beat para o primeiro plano executável.
- **Racional**: a especificação lista essa combinação como stack recomendada e os sprints exigem filas, retentativas e lembretes. `RQ`/`arq` ficam como alternativas caso surja uma restrição concreta.
- **Alternativas consideradas**: RQ ou arq; não foram escolhidos porque a documentação não apresenta vantagem específica para este projeto.

## 3. Interface de operação

- **Decisão de planejamento**: começar API-first e não incluir uma aplicação web dedicada no MVP.
- **Racional**: o escopo pede uma “interface mínima”, mas a especificação não descreve telas, navegação ou fluxos de UI; as rotas e OpenAPI são definidas. A API, a documentação e as demonstrações no Mailpit permitem validar o núcleo sem inventar requisitos de interface.
- **Alternativas consideradas**: painel web operacional; permanece fora da implementação planejada até que os fluxos/telas sejam definidos.

## 4. ORM e migrações

- **Decisão**: manter a escolha técnica pendente na inicialização; adotar ferramenta Python de migração e persistência compatível com PostgreSQL sem alterar os contratos do domínio.
- **Racional**: a especificação define PostgreSQL e entidades, mas não seleciona ORM nem ferramenta de migração. Definir a ferramenta ao criar o projeto é necessário para tarefas executáveis, mas não deve ser confundido com uma decisão presente na fonte.
- **Alternativas consideradas**: SQLAlchemy + Alembic como opção provável; outra camada equivalente pode ser selecionada na configuração inicial.

## Itens ainda a confirmar antes do código de produção

- Provedor real selecionado após comparação em staging e verificação de planos vigentes.
- Framework de persistência/migrações na inicialização do repositório.
- Se a “interface mínima” significa apenas API/OpenAPI ou se deve haver UI operacional.
- Domínio autenticado e destinatários de teste disponíveis para staging real.
