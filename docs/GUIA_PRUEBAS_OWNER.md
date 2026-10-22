# Guía para el dueño: prueba en vivo (M1) e importación histórica (M2)

Tres tareas, en este orden. Cada una genera un archivo de evidencia que yo reviso.
Todo se hace en **PowerShell**, en tu PC con Windows.

| # | Tarea | Para qué | Duración aprox. |
|---|---|---|---|
| 0 | Preparación (una sola vez) | Instalar herramientas y bajar el código | 10–15 min |
| 1 | Chequeo de conexión con Binance | Confirmar que tu red llega a spot y futuros | 1 min |
| 2 | Prueba en vivo de 2 horas | Cierra el hito M1 | 2 h 10 min |
| 3 | Importación histórica | Cierra el hito M2; habilita los backtests reales | varias horas (puede correr de noche) |

Haz la 2 y la 3 **una después de la otra, no al mismo tiempo**: ambas usan la carpeta `data\` y la
red, y una sola instancia de la app puede usar esa carpeta.

---

## 0. Preparación (una sola vez)

Abre **PowerShell** (menú Inicio → escribe "PowerShell" → Enter).

### 0.1 Instalar las herramientas
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
winget install OpenJS.NodeJS.LTS
winget install Git.Git
```
- `uv` instala Python 3.13 y las librerías del proyecto.
- Node.js construye el dashboard la primera vez.
- Git baja el código y sube la evidencia.

**Cierra PowerShell y ábrelo de nuevo** para que reconozca los programas nuevos. Comprueba:
```powershell
uv --version
node --version
git --version
```
Si alguno dice "no se reconoce", reinicia la PC y vuelve a probar.

### 0.2 Bajar el código
Elige una carpeta, por ejemplo tus Documentos:
```powershell
cd $HOME\Documents
git clone https://github.com/felipemartinez888/trading-system-claude
cd trading-system-claude
```
Si ya lo habías clonado antes, en su lugar actualiza:
```powershell
cd $HOME\Documents\trading-system-claude
git pull
```

### 0.3 Configurar tu identidad en git (para subir la evidencia)
```powershell
git config --global user.name "Felipe Martinez"
git config --global user.email "felipe16csh@gmail.com"
```
La primera vez que hagas `git push`, Windows abrirá una ventana para iniciar sesión en GitHub.

### 0.4 Mantener la PC despierta
Durante la prueba de 2 horas y la importación la PC no puede suspenderse:
Configuración → Sistema → Inicio/apagado y suspensión → "Suspender" en **Nunca** (mientras dure).
Si es portátil, déjala **enchufada**.

> De aquí en adelante, **todos los comandos se ejecutan dentro de la carpeta
> `trading-system-claude`**. Si abres una ventana nueva, primero:
> `cd $HOME\Documents\trading-system-claude`

---

## 1. Chequeo de conexión con Binance (1 minuto)

```powershell
uv run python scripts/spikes/binance_check.py
```
La primera vez tarda unos minutos porque instala las dependencias.

**Qué esperar:** una línea por cada prueba con `OK` o `FAIL`, y al final `all checks passed` o
la lista de fallos. Se guarda un JSON en `evidence\spikes\`.

**Si algo falla:**
- `fut_...` o `ws_fut_...` en FAIL → tu red o tu país podría no tener acceso a futuros. Avísame:
  el sistema puede trabajar solo con spot.
- Todo en FAIL → revisa internet, VPN o antivirus/firewall bloqueando Python.

---

## 2. Prueba en vivo de 2 horas (cierra M1)

### 2.1 Iniciar
```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_live.ps1
```
- Se abre el dashboard en el navegador (`http://127.0.0.1:8765`) y el monitoreo arranca solo.
- **No cierres** la ventana de PowerShell ni abras otra copia de la app.
- ¿Quieres probar primero que todo funciona? Haz una corrida corta de 15 minutos:
  `powershell -ExecutionPolicy Bypass -File scripts\verify_live.ps1 -Hours 0.25`

### 2.2 Qué mirar mientras corre (opcional pero útil)
- **Barra superior:** Spot y Futuros en verde (HEALTHY).
- **Markets:** precios de BTC, ETH y SOL y el gráfico de velas de 1 minuto.
- **System → Health:** estado de cada stream, huecos y desfase del reloj.

### 2.3 Prueba de desconexión (a mitad de la corrida, ~1 hora después de iniciar)
1. Desconecta internet **1 minuto** (Wi-Fi apagado o cable desconectado).
2. Vuelve a conectarlo.
3. Deberías ver los streams pasar a DISCONNECTED → RECOVERING → HEALTHY y una notificación.
   Los minutos perdidos se recuperan solos en unos minutos.

### 2.4 Al terminar
Pasadas las 2 horas la app se cierra sola y muestra el resultado:

| Código de salida | Significado |
|---|---|
| `0` | **Aprobado** |
| `1` | El monitoreo no arrancó (el motivo aparece en pantalla) |
| `2` | Terminó, pero algún criterio falló (el reporte dice cuál) |

El reporte queda en `evidence\live\verify-<fecha>.json`. **Súbelo aunque haya fallado**: el fallo
también me sirve para corregir.

### 2.5 Prueba de cierre forzado (aparte, 5 minutos)
1. Abre la app normal: doble clic en `scripts\TradingSystemClaude.cmd`, y pulsa **Start**.
2. Espera 2 minutos.
3. Abre el Administrador de tareas (Ctrl+Shift+Esc), busca `python`, clic derecho → **Finalizar tarea**.
4. Vuelve a abrir `scripts\TradingSystemClaude.cmd`.
5. En **System → Health** debe aparecer el aviso de que la sesión anterior terminó con un crash,
   con el intervalo no observado, y las velas faltantes recuperadas.
6. Haz una captura de pantalla de ese aviso.

Prueba también cerrar la ventana de la consola con la **X**: al reabrir debería decir
"ended cleanly". Si dice crash, avísame (es el único caso que no se puede probar en la nube).

---

## 3. Importación histórica (cierra M2)

### 3.1 Iniciar
```powershell
powershell -ExecutionPolicy Bypass -File scripts\import_history.ps1
```
Qué hace, en orden:
1. Calcula el **universo de investigación** congelado al 1 de enero de 2025
   (`configs\universe_snapshot_2025-01-01.yaml`) y el universo de hoy, solo para monitoreo en vivo.
2. Descarga velas de 1 minuto de spot y futuros desde 2022, precios mark y funding.
   **Cada archivo se verifica con su checksum.**
3. Verifica todos los datasets.
4. Guarda los resúmenes en `evidence\data\<fecha>\`.

### 3.2 Cuánto tarda y cuánto ocupa
- **Varias horas.** Lo más lento son los precios mark de futuros por REST, unos 10 minutos por
  símbolo. Lo ideal es dejarlo corriendo de noche.
- Disco: reserva unos **10 GB** libres.
- Si se corta (luz, internet), **vuelve a correr el mismo comando**: retoma desde donde iba sin
  bajar de nuevo lo que ya está verificado.

### 3.3 Al terminar
El final de la pantalla dice `Summary written to ... (failed steps: N)`.
- `failed steps: 0` → perfecto.
- Más de 0 → no pasa nada grave: súbelo igual y yo reviso qué falló.

Los datos en sí quedan en `data\` y **no se suben** a GitHub (son demasiado grandes y están
excluidos a propósito). Solo se suben los resúmenes y el archivo del universo.

---

## 4. Subir la evidencia a GitHub

Cuando termines cualquiera de las pruebas (puedes subir cada una por separado):
```powershell
git pull
git add evidence configs\universe_snapshot_2025-01-01.yaml configs\universe_live_*.yaml
git commit -m "evidence: owner runs (binance check, live 2h, history import)"
git push
```
Si `git add` se queja de que un archivo de `configs` no existe, quítalo del comando: depende de
qué pruebas hiciste.

**Alternativa sin git:** adjunta en este chat los JSON de `evidence\spikes\`, `evidence\live\` y
`evidence\data\<fecha>\summary.json`, y la captura de pantalla del paso 2.5.

---

## 5. Problemas frecuentes

| Síntoma | Solución |
|---|---|
| `uv no se reconoce` | Cierra y vuelve a abrir PowerShell; si persiste, reinicia la PC. |
| "La ejecución de scripts está deshabilitada" | Usa siempre `powershell -ExecutionPolicy Bypass -File ...` como en esta guía. |
| `Refused to start` o código 3 | Ya hay otra copia de la app abierta o el puerto 8765 está ocupado: cierra la otra ventana. |
| El navegador muestra "dashboard bundle is not built" | Falta Node.js: instálalo (paso 0.1) y vuelve a lanzar. |
| Antivirus bloquea Python | Permite `python.exe` dentro de la carpeta `.venv` del proyecto. |
| `git push` pide usuario y contraseña | Inicia sesión en la ventana de GitHub que se abre; no uses tu contraseña en la terminal. |

Ante cualquier error, copia el texto rojo de la pantalla y pégamelo aquí.
