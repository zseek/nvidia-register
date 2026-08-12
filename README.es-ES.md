# nvidia-register

Registro automático de cuentas NVIDIA BUILD y creación de claves API

## Características principales

- **Proceso completamente automatizado**: crear correo temporal → registrar → resolver captcha → crear organización → generar clave → registrar en CSV, todo automatizado
- **Registro por lotes**: admite registrar múltiples cuentas en una sola ejecución, con preguntas interactivas o especificando el número con `-n` parámetro
- **Captcha**: admite resolución manual (`manual`) y automática (`yescaptcha`, `captcharun`)
- **Servicio de correo**: admite `cloudflare_temp_email` (autoimplementado) y `duckmail` (API de DuckMail)
- **Contraseña aleatoria**: genera automáticamente contraseñas de 12 caracteres (mayúsculas, minúsculas y números)
- **Saltar verificación de teléfono**: usar el nombre de organización durante el registro para evitar la necesidad de número de teléfono, y generar una clave API de larga duración
- **Registro en CSV**: agrega inmediatamente `correo,password,apikey` al archivo CSV tras registrar exitosamente
- **Salida elegante**: `Ctrl+C` finaliza seguro al completar la cuenta actual

## Estructura del proyecto

```
├── main.py              # Entrada principal + orquestación del proceso
├── config.py            # Carga de configuración (config.toml)
├── email_providers.py   # Capa abstracta de servicios de correo temporal
├── captcha.py           # Procesamiento de captcha
├── passwords.py         # Generación de contraseñas aleatorias
├── records.py           # Escritura de registros en CSV
├── config.toml          # Archivo de configuración
└── config.toml.example  # Ejemplo de configuración
```

## Requisitos previos

- Python 3.11+
- Navegador Chromium (descargado automáticamente por Playwright)
- **Servicio de correo temporal** (soportado `cloudflare_temp_email` autoimplementado y `duckmail`)
- (Opcional) [YesCaptcha](https://yescaptcha.com/i/57yzUt) / [CaptchaRun](https://captcha-run.com/sso?inviter=ad8fbc2f-9721-430e-87a9-1898fa0177b4) claves API

## Instalación

```bash
pip install -r requirements.txt
playwright install chromium
```

## Configuración

```bash
# Generar plantilla de archivo de configuración
python main.py --init
```

Editar el archivo `config.toml` generado:

```toml
email_provider = "cloudflare_temp_email"

[cloudflare_temp_email]
api_url = "https://mail.your-server.com"
admin_auth = "your_admin_key"
domain = "your-domain.com"

[duckmail]
api_url = "https://api.duckmail.sbs"
domain = "duckmail.sbs"
api_key = ""

[captcha]
mode = "manual" # manual | yescaptcha | captcharun
yescaptcha_client_key = ""
yescaptcha_api_url = "https://api.yescaptcha.com"
captcharun_token = ""
captcharun_api_url = "https://api.captcha-run.com"
poll_interval_seconds = 3
timeout_seconds = 180

[nvidia]
output_csv = "accounts.csv"
key_name = "api"
account_name = "NVIDIA Build"
key_expiry_date = "2126-05-08T08:00:00Z"

[browser]
headless = false
close_delay_seconds = 5
```

| Configuración | Descripción |
|----------------|-------------|
| `email_provider` | Tipo de servicio de correo temporal (soportado `cloudflare_temp_email` / `duckmail`) |
| `cloudflare_temp_email.api_url` | Dirección de API del servicio de correo |
| `cloudflare_temp_email.admin_auth` | Clave de administrador del servicio de correo |
| `cloudflare_temp_email.domain` | Dominio del correo |
| `duckmail.api_url` | Dirección de API de DuckMail (por defecto `https://api.duckmail.sbs`) |
| `duckmail.domain` | Dominio de correo de DuckMail, como `duckmail.sbs` o tu dominio personalizado |
| `duckmail.api_key` | Clave API para dominios privados de DuckMail, dejar vacío si usas dominio público |
| `captcha.mode` | `manual` para resolver manualmente, `yescaptcha` usando API de YesCaptcha, `captcharun` usando API de CaptchaRun |
| `captcha.yescaptcha_client_key` | Clave de cliente de YesCaptcha (requerido si mode=yescaptcha) |
| `captcha.yescaptcha_api_url` | Dirección de API de YesCaptcha (por defecto `https://api.yescaptcha.com`) |
| `captcha.captcharun_token` | Token de autorización de CaptchaRun (requerido si mode=captcharun) |
| `captcha.captcharun_api_url` | Dirección de API de CaptchaRun (por defecto `https://api.captcha-run.com`) |
| `captcha.poll_interval_seconds` | Intervalo en segundos para consultar resultados de captcha |
| `captcha.timeout_seconds` | Tiempo máximo de espera en segundos para resolver el captcha |
| `nvidia.output_csv` | Ruta del archivo CSV donde se registran los datos |
| `nvidia.key_name` | Nombre de la clave API |
| `nvidia.account_name` | Nombre ingresado al crear la organización (para saltarse la verificación de teléfono) |
| `nvidia.key_expiry_date` | Fecha de expiración de la clave API (por defecto ~100 años) |
| `browser.headless` | Ejecutar el navegador en modo sin cabeza |
| `browser.close_delay_seconds` | Retardo en segundos antes de cerrar el navegador al finalizar |

## Uso

```bash
# Preguntar interactivamente cuántas cuentas registrar
python main.py

# Especificar cantidad de cuentas a registrar (sin preguntar)
python main.py -n 5
python main.py --count 3
```

Durante el registro por lotes, cada cuenta usa una sesión independiente del navegador, con un intervalo de 5 segundos. `Ctrl+C` detiene elegante el proceso: completa la cuenta actual en curso y muestra un resumen de éxitos/fracasos.

Cada registro exitoso se agrega automáticamente al archivo `accounts.csv`:

```csv
email,password,apikey
nv12345678@your-domain.com,aB3dE5fG7hI9,nvapi-xxxx...
```

## Flujo de registro

```
build.nvidia.com (ingresar correo) → login.nvgs.nvidia.com (ingresar contraseña + hCaptcha)
→ página de captcha (ingresar teclado) → página de consentimiento (enviar) → crear organización (saltarse verificación de teléfono)
→ API NGC (generar clave) → registro en CSV
```

## Extender servicios de correo

Actualmente se soportan `cloudflare_temp_email` y `duckmail`, y pueden agregarse nuevos proveedores implementando el protocolo `TempEmailProvider`:

```python
class TempEmailProvider(Protocol):
    def create_inbox(self, name: str) -> TempEmailInbox: ...
    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None: ...
```

Agregar el nuevo proveedor en `email_providers.py` e incluirlo en `build_email_provider()` para activarlo.

## Notas importantes

- El modo **manual** de hCaptcha requiere intervención humana
- El registro incluye consultas de captcha (máximo 3 minutos)
- La ventana del navegador se cierra automáticamente al finalizar (configurable el retardo)
- Durante registros por lotes, cada cuenta tiene una sesión independiente del navegador, sin interferencias
- El segundo `Ctrl+C` fuerza la salida inmediata
