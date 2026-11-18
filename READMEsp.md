# RCommander alfa

[English](README.md)

Gestor de archivos de dos paneles para Debian/WSL, inspirado en Midnight Commander.

## Ejecutar

```bash
cd ~/proyectos/rcomander
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

## Teclas

| Tecla | Acción |
|---|---|
| Flechas / Enter | Seleccionar y abrir directorio |
| Tab | Cambiar de panel |
| Insert | Marcar o desmarcar el elemento actual y avanzar |
| F1 / F2 | Configurar el panel izquierdo / derecho como local o SSH |
| F3 | Ir a una ruta local o remota |
| F4 | Editar el archivo seleccionado con Vim, localmente o mediante SSH |
| F5 | Copiar al otro panel: elegir rsync, SCP o copia directa local |
| F6 | Mover al otro panel, solo local |
| F7 | Crear un directorio en el panel activo (local o remoto) |
| F8 | Eliminar el archivo o directorio seleccionado después de confirmarlo |
| F9 | Actualizar ambos paneles |
| F10 | Salir |
| Ctrl+S | Abrir una consola SSH; `exit` vuelve a RCommander |
| Ctrl+X | Cancelar una transferencia SCP o rsync en curso |

En los diálogos de opciones se puede cambiar de botón con las flechas o Tab y seleccionar con Enter. F8 elimina directorios recursivamente, incluido todo su contenido; los enlaces simbólicos se eliminan sin borrar el directorio al que apuntan. Se copia el elemento seleccionado, incluidos directorios. Las copias pueden sobrescribir archivos existentes. Rsync utiliza `-a` y no elimina archivos adicionales del destino. Para editar se necesita Vim instalado en la máquina donde se encuentra el archivo. Sal de Vim con `:wq` para guardar o `:q!` para descartar.

F5, F6 y F8 actúan sobre todos los elementos marcados del panel activo, o sobre la fila actual si no hay marcas. Las marcas aparecen con `*`, se conservan al actualizar y se limpian al cambiar de directorio o tras una operación completa. En caso de error o cancelación se conservan las marcas de elementos que sigan en el panel. La eliminación de directorios es recursiva.

Los tamaños se muestran en unidades binarias (KiB, MiB, GiB). Al copiar con rsync se muestra el progreso global informado por rsync; con SCP se muestra el progreso del archivo actual, por lo que al copiar directorios puede volver a empezar para cada archivo. SCP entre dos rutas locales puede no informar de progreso. La copia directa no muestra porcentaje ni permite cancelación con Ctrl+X. Al cancelar SCP o rsync pueden quedar archivos parciales o ya copiados en el destino.

## SSH

Instala `sshpass` para usar contraseña:

```bash
sudo apt install sshpass
```

Con F1 o F2 selecciona «Remoto SSH» e introduce `usuario@servidor` o un alias de `~/.ssh/config` (que puede configurar un puerto personalizado) y la contraseña. El panel abrirá el directorio inicial remoto (`.`). Deja la contraseña vacía para usar clave o agente SSH. La contraseña se mantiene en memoria mientras el panel esté conectado y se pasa mediante el entorno a `sshpass`, sin guardarla en disco. Para volver a local, pulsa F1 o F2 y selecciona «Local».

Ctrl+S abre una consola SSH interactiva. Si el panel activo es remoto, utiliza su conexión y comienza en su directorio actual; al salir se actualiza el panel. Si el panel es local, pide servidor y contraseña y abre una consola independiente. Escribe `exit` o pulsa Ctrl+D para regresar a la aplicación.

La primera conexión acepta automáticamente claves de host desconocidas; si una clave conocida cambia, SSH rechazará la conexión. Puedes verificar la huella previamente conectándote con `ssh usuario@servidor`. La navegación requiere Python 3 en el servidor remoto; rsync también debe estar instalado en ambos extremos para utilizarlo. Esta alfa admite un solo extremo remoto por transferencia.
