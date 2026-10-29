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

| Perfil | Login | Senha / CPF |
|---|---|---|
| Aluno (João de Teste) | matrícula `2025999` | CPF `52998224725` (fictício) |
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
| Superusuário | `/superusuario` | Cadastra membros do colegiado e coordenadores(as) com senha inicial e desativa acessos. Na aba **Alunos** (`/superusuario/alunos`), cadastra alunos (com ou sem CPF), busca alunos e edita nome, matrícula, curso, currículo e CPF, além de bloquear ou liberar o acesso |

Cursos que podem ser vinculados a um(a) coordenador(a): Engenharia Automotiva, Engenharia Aeroespacial, Engenharia Eletrônica, Engenharia de Energia e Engenharia de Software (lista fixa em `CURSOS_FCTE`, no `app.py`). O aluno escolhe um desses cursos no primeiro acesso.

Servidores cadastrados pelo superusuário recebem uma **senha inicial** e, no primeiro acesso, são obrigados a criar uma senha pessoal antes de usar o sistema. O mesmo vale quando o superusuário redefine a senha de alguém. Depois disso, cada servidor pode trocar a senha quando quiser pelo link **Alterar senha**, no topo da página. As contas de teste não passam por essa troca obrigatória.

O primeiro superusuário real é criado pelo terminal:

```bash
python -m flask --app app criar-superusuario nome@unb.br "Nome Completo"
```

## Configuração inicial

1. **Alunos** — não há lista prévia: qualquer aluno se cadastra pelo **primeiro acesso**, informando nome completo, matrícula, curso (um dos cinco da FCTE), número do currículo e CPF. O superusuário também pode cadastrar alunos na aba **Alunos**.

2. **Professores credenciados** (só e-mails `@unb.br`):

   ```bash
   flask --app app criar-professor nome.sobrenome@unb.br "Prof. Nome Sobrenome"
   flask --app app descredenciar-professor nome.sobrenome@unb.br
   ```

## Executar em desenvolvimento

```bash
python app.py
```

- Alunos: http://127.0.0.1:5000/
- Colegiado, coordenação e superusuário: http://127.0.0.1:5000/colegiado

O sistema **não envia e-mails**. O aluno acompanha o resultado de cada certificado (validado, com horas e reunião, ou recusado, com o motivo) no próprio painel.

## Como funciona o acesso do aluno

- O aluno entra com **matrícula e CPF**, sem senha. Não há opção de "esqueci minha senha".
- Matrícula e CPF são aceitos **somente com números**. No navegador, letras, pontos, traços e barras são descartados ao digitar ou colar; o servidor também recusa qualquer outro caractere.
- O CPF passa pela conferência dos dígitos verificadores e fica guardado no banco de forma cifrada (hash), como uma senha. Por isso ele não aparece em nenhuma tela; o superusuário pode apenas informar um CPF novo.
- Uma matrícula com cadastro concluído não pode ser recadastrada pelo primeiro acesso. Em caso de erro no CPF, o superusuário corrige na aba **Alunos**.
- Alunos cadastrados antes desta versão (sem CPF) completam o cadastro pelo primeiro acesso.
- Professores e coordenadores entram com e-mail institucional e senha. Se esquecerem a senha, um superusuário a redefine na página de administração (no próximo acesso, a pessoa cria uma senha nova). Um superusuário que esqueça a própria senha pode redefini-la pelo terminal com `python -m flask --app app criar-superusuario email@unb.br "Nome"`.

## Arquivo de configuração `.env`

As senhas e chaves ficam num arquivo chamado `.env`, na mesma pasta do `app.py`, sem precisar digitar nada no terminal:

1. Faça uma cópia do arquivo `.env.exemplo` e renomeie a cópia para `.env`.
2. Abra o `.env` no Bloco de Notas (ou no VS Code), preencha os valores e salve.
3. Inicie o sistema normalmente (`python app.py`). Ele lê o `.env` sozinho.

O `.env` está no `.gitignore` e **não deve ir para o GitHub**. Se uma variável também estiver definida no sistema operacional, o valor do sistema tem prioridade. A `SECRET_KEY` não precisa ser preenchida: se faltar, o sistema cria uma e a guarda em `instance/secret_key`.

## Variáveis de ambiente

Todas as variáveis abaixo podem ser colocadas no `.env`.

| Variável | Padrão | Descrição |
|---|---|---|
| `SECRET_KEY` | criada e guardada em `instance/secret_key` | Chave das sessões |
| `DATABASE` | `instance/certificados.db` | Banco SQLite |
| `UPLOAD_FOLDER` | `uploads/` | Onde os PDFs são guardados quando o R2 não está configurado |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | — | Guardam os PDFs no Cloudflare R2 (ver seção abaixo) |
| `DOMINIO_PROFESSOR` | `unb.br` | Domínio exigido para professores |
| `CONTAS_TESTE` | `1` | Ativa as contas de teste (use `0` em produção) |
| `SESSION_COOKIE_SECURE` | `0` | Use `1` atrás de HTTPS |

## Guardar os PDFs no Cloudflare R2

Com as quatro variáveis `R2_*` definidas, os PDFs vão para um bucket privado do Cloudflare R2, e o servidor guarda só o banco de dados. Os PDFs continuam passando pelo sistema, que confere quem pode ver cada um. Sem essas variáveis, o sistema usa a pasta `uploads/`.

1. No painel da Cloudflare, abra **R2 Object Storage** e ative o serviço (a Cloudflare pode pedir um cartão para verificação; o plano gratuito inclui 10 GB).
2. Crie um bucket, por exemplo `certificados-fcte`. **Não** ative o acesso público.
3. Em **Manage R2 API Tokens**, crie um token com permissão **Object Read & Write**, restrito a esse bucket. Copie o *Access Key ID* e o *Secret Access Key* (o segredo aparece uma única vez).
4. Copie o **Account ID**, que aparece no painel do R2.
5. Preencha `R2_ACCESS_KEY_ID` e `R2_SECRET_ACCESS_KEY` no arquivo `.env` (ver "Arquivo de configuração .env") e teste: `python -m flask --app app testar-armazenamento`.
6. Se já houver PDFs na pasta `uploads/`, copie-os para o R2 uma vez: `python -m flask --app app enviar-pdfs-para-r2`.

## Produção

```bash
export SESSION_COOKIE_SECURE=1   # demais configurações no .env
gunicorn -w 3 -b 127.0.0.1:8000 app:app
```

Coloque um Nginx (ou similar) com HTTPS na frente e configure `client_max_body_size 26M;`. Faça backup periódico de `instance/certificados.db` e, se não usar o R2, da pasta `uploads/`.

## Regras implementadas

- Upload apenas de PDF (extensão + assinatura `%PDF`), até 25 MB, com horas, data de início/término e tipo de atuação.
- Os arquivos enviados são renomeados para **`<matrícula>_<curso>_<nº>.pdf`** (ex.: `2025999_Engenharia_de_Software_1.pdf`), com o curso sem acentos. O número de cada certificado é fixo: excluir um não muda o nome dos outros. Se o superusuário mudar a matrícula ou o curso do aluno, os arquivos são renomeados automaticamente. Arquivos enviados em versões anteriores são renomeados na primeira execução desta versão.
- Só são aceitos certificados em que o aluno atuou como **membro da equipe organizadora**; certificados de **ouvinte** são bloqueados no navegador e no servidor.
- O aluno pode **excluir** certificados aguardando avaliação ou recusados (o PDF é apagado). Certificados **validados** não podem ser excluídos.
- Status: *Aguardando avaliação do colegiado de extensão*, *Validado* ou *Recusado* (com motivo obrigatório, exibido ao aluno).
- Ao validar, o professor informa a **carga horária validada** (até o total declarado pelo aluno) e o **número da reunião do colegiado de extensão**, que fica visível ao aluno; o painel do aluno soma só as horas validadas.
- Painel do professor com filtro por curso e por situação; registra quem avaliou e quando; permite reavaliar.
- Senhas com hash, proteção CSRF em todos os formulários, PDFs acessíveis só pelo dono e por professores.
