# Envio de Certificados de Extensão — FCTE/UnB

Sistema em Flask + SQLite para alunos enviarem certificados de atividades de extensão e para o colegiado de extensão avaliá-los.

## Instalação

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Contas de teste

Criadas automaticamente na primeira execução:

| Perfil | Login | Senha |
|---|---|---|
| Aluno (João de Teste) | matrícula `2025999` | `123456` |
| Membro do colegiado de extensão | `teste@unb.br` | `123456` |
| Coordenador(a) de curso (Engenharia de Software) | `coordenador@unb.br` | `123456` |
| Superusuário | `admin@unb.br` | `123456` |

Em produção, defina `CONTAS_TESTE=0` para desativá-las.

> **Windows:** se `flask` não for reconhecido, use `python -m flask` nos comandos abaixo.

## Perfis de acesso

Todos os servidores entram por http://127.0.0.1:5000/colegiado com e-mail institucional e senha, e o sistema leva cada um à sua área. Quem tem mais de um perfil alterna entre as áreas pelo menu do topo.

| Perfil | Área | O que faz |
|---|---|---|
| Membro do colegiado de extensão | `/colegiado/certificados` | Vê os certificados **agrupados por aluno** e valida (com carga horária e nº da reunião) ou recusa cada um |
| Coordenador(a) de curso | `/coordenacao` | Vê os certificados **validados** dos cursos que coordena, marca cada um como **lançado no histórico do aluno** e acompanha o total de horas por aluno |
| Superusuário | `/superusuario` | Cadastra membros do colegiado e coordenadores(as) com senha inicial e desativa acessos. Na aba **Alunos** (`/superusuario/alunos`), cadastra alunos (com ou sem senha inicial), busca alunos e edita nome, matrícula, curso, currículo e senha, além de bloquear ou liberar o acesso |

Cursos que podem ser vinculados a um(a) coordenador(a): Engenharia Automotiva, Engenharia Aeroespacial, Engenharia Eletrônica, Engenharia de Energia e Engenharia de Software (lista fixa em `CURSOS_FCTE`, no `app.py`). O aluno escolhe um desses cursos no primeiro acesso.

Servidores cadastrados pelo superusuário recebem uma **senha inicial** e, no primeiro acesso, são obrigados a criar uma senha pessoal antes de usar o sistema. O mesmo vale quando o superusuário redefine a senha de alguém. Depois disso, cada servidor pode trocar a senha quando quiser pelo link **Alterar senha**, no topo da página. As contas de teste não passam por essa troca obrigatória.

O primeiro superusuário real é criado pelo terminal:

```bash
python -m flask --app app criar-superusuario nome@unb.br "Nome Completo"
```

## Configuração inicial

1. **Alunos** — não há lista prévia: o superusuário pode cadastrar alunos na aba **Alunos**, e qualquer aluno também pode se cadastrar pelo **primeiro acesso**, informando nome completo, matrícula, curso (um dos cinco da FCTE) e número do currículo. O link para criar a senha vai para o e-mail institucional `matrícula@aluno.unb.br`, o que garante que só o dono da matrícula conclui o cadastro. Enquanto a senha não for criada, o aluno pode refazer o primeiro acesso para corrigir os dados.

2. **Professores credenciados** (só e-mails `@unb.br`):

   ```bash
   flask --app app criar-professor nome.sobrenome@unb.br "Prof. Nome Sobrenome"
   flask --app app descredenciar-professor nome.sobrenome@unb.br
   ```

## Executar em desenvolvimento

```bash
export SECRET_KEY="uma-chave-longa-e-aleatoria"
export MODO_DEV=1
flask --app app run --debug
```

- Alunos: http://127.0.0.1:5000/
- Colegiado, coordenação e superusuário: http://127.0.0.1:5000/colegiado

Com `MODO_DEV=1` e sem SMTP configurado, os links de criação/redefinição de senha aparecem na tela e no terminal, em vez de serem enviados por e-mail.

## Como funciona a senha do aluno

- Não há regra de formato para a senha: basta não estar vazia.
- **Primeiro acesso** e **Esqueci minha senha** enviam um link (válido por 60 min, uso único) para o e-mail institucional `matrícula@aluno.unb.br`. Isso garante que só o dono da matrícula consegue criar a senha.
- Se o padrão de e-mail for outro, ajuste `EMAIL_ALUNO_PADRAO` (ex.: `{matricula}@aluno.unb.br`).
- O superusuário pode definir uma nova senha para qualquer aluno na aba **Alunos**. Pelo terminal também é possível apagar a senha (o aluno refaz o primeiro acesso): `flask --app app resetar-senha-aluno 2025187`.
- Professores também recuperam a senha por link enviado ao e-mail institucional.

## Variáveis de ambiente

| Variável | Padrão | Descrição |
|---|---|---|
| `SECRET_KEY` | aleatória a cada início | **Obrigatória em produção.** Sem ela, sessões e links caem a cada reinício. |
| `DATABASE` | `instance/certificados.db` | Banco SQLite |
| `UPLOAD_FOLDER` | `uploads/` | Onde os PDFs são guardados |
| `EMAIL_ALUNO_PADRAO` | `{matricula}@aluno.unb.br` | E-mail institucional do aluno |
| `DOMINIO_PROFESSOR` | `unb.br` | Domínio exigido para professores |
| `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_FROM`, `MAIL_USE_TLS` | — | SMTP para envio dos links |
| `MODO_DEV` | `0` | Mostra links na tela quando não há SMTP |
| `CONTAS_TESTE` | `1` | Ativa as contas de teste (use `0` em produção) |
| `SESSION_COOKIE_SECURE` | `0` | Use `1` atrás de HTTPS |

## Produção

```bash
export SECRET_KEY=...  MAIL_SERVER=...  SESSION_COOKIE_SECURE=1
gunicorn -w 3 -b 127.0.0.1:8000 app:app
```

Coloque um Nginx (ou similar) com HTTPS na frente e configure `client_max_body_size 26M;`. Faça backup periódico de `instance/certificados.db` e da pasta `uploads/`.

## Regras implementadas

- Upload apenas de PDF (extensão + assinatura `%PDF`), até 25 MB, com horas, data de início/término e tipo de atuação.
- Certificados de **ouvinte** são bloqueados no navegador e no servidor.
- Status: *Aguardando avaliação do colegiado de extensão*, *Validado* ou *Recusado* (com motivo obrigatório, exibido ao aluno).
- Ao validar, o professor informa a **carga horária validada** (até o total declarado pelo aluno) e o **número da reunião do colegiado de extensão**, que fica visível ao aluno; o painel do aluno soma só as horas validadas.
- Ao validar ou recusar, o aluno recebe um **e-mail** com o resultado, a carga horária validada ou o motivo da recusa.
- Painel do professor com filtro por curso e por situação; registra quem avaliou e quando; permite reavaliar.
- Senhas com hash, proteção CSRF em todos os formulários, PDFs acessíveis só pelo dono e por professores.
