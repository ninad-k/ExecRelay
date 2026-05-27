# IDE Troubleshooting Guide

## Configurations Not Appearing in Run Dropdown

**Symptoms:** Run dropdown is empty or shows fewer than 13 configs.

**Solution:**
1. Close GoLand/IntelliJ completely
2. Delete IDE cache:
   ```bash
   rm -rf ~/.cache/JetBrains/*  # Linux
   rm -rf ~/Library/Caches/JetBrains*  # macOS
   rmdir %APPDATA%\JetBrains\*  # Windows
   ```
3. Reopen GoLand/IntelliJ
4. IDE will rescan `.run/` directory and load all 13 configs

## "Module 'execrelay' not found"

**Symptoms:** Configuration won't run, error: "Module 'execrelay' not found"

**Solution:**
1. **File** → **Project Structure** → **Project** → **Project Name**
2. Verify it says **"execrelay"**
3. If not, set it to **"execrelay"**
4. Click **OK**
5. Go to **File** → **Invalidate Caches** → **Invalidate and Restart**

Alternatively:
1. Close all configuration windows
2. Right-click `go.mod` in file browser → **Mark as Root**
3. Go to **Run** → **Edit Configurations** again

## Go Services Won't Compile

**Symptoms:**
- Error: "cannot find package"
- Or: "GOROOT not set"

**Solution A - Set GOROOT:**
1. Go to **GoLand** → **Preferences** (or **Settings** on Linux/Windows)
2. Navigate to **Go** → **Go Modules**
3. Verify "Go Modules" is enabled
4. Click **File** → **Project Structure** → **SDKs**
5. If no Go SDK listed, click **+** → **Add SDK** → **Go SDK**
6. Point to your Go installation (e.g., `/usr/local/go` or `/usr/bin/go`)
7. Click **OK**

**Solution B - Check environment:**
```bash
which go
go version  # Should be 1.21+
```

If Go not installed:
- macOS: `brew install go`
- Linux: `sudo apt-get install golang-go`
- Windows: Download from https://golang.org/dl

## Python Services Won't Start

**Symptoms:**
- Error: "No Python interpreter selected"
- Or: `ModuleNotFoundError: No module named 'asyncpg'`

**Solution A - Select Python interpreter:**
1. Go to **PyCharm** (if using PyCharm) or **GoLand** → **Preferences** → **Project: execrelay** → **Python Interpreter**
2. Click the gear icon ⚙️ → **Add**
3. Select **"Existing environment"** → find your Python 3.10+ binary
4. Click **OK**

**Solution B - Install dependencies:**
```bash
cd /Users/ninadk/GolandProjects/ExecRelay
pip install asyncpg fastapi uvicorn prometheus-client nats-py
```

Or use PyCharm's package installer:
1. Open any `.py` file
2. IDE shows "Missing dependencies" → click **Install requirements**

## "Address already in use" on Port 8080

**Symptoms:** When launching Ingress, error: `bind: address already in use`

**Solution:**
```bash
# Find what's using port 8080
lsof -i :8080

# Kill it
kill -9 <PID>

# Or use a different port:
# 1. Run → Edit Configurations
# 2. Select "01. Ingress"
# 3. Find Environment variables: HTTP_ADDR=:8080
# 4. Change to HTTP_ADDR=:9080
# 5. Apply & run
```

Then update health check commands to use new port:
```bash
curl http://localhost:9080/health
```

## Database Connection Fails

**Symptoms:** Python services log `psycopg2.OperationalError: could not connect to server`

**Solution:**

1. **Check if PostgreSQL running:**
   ```bash
   docker ps | grep postgres
   ```

2. **If not running, start it:**
   ```bash
   docker run -d \
     -e POSTGRES_PASSWORD=execrelay_dev_password \
     -e POSTGRES_DB=execrelay \
     -p 5432:5432 \
     --name execrelay-postgres \
     postgres:14
   ```

3. **Check connection:**
   ```bash
   psql postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay -c "SELECT 1"
   ```

4. **If psql not found:**
   - macOS: `brew install postgresql`
   - Linux: `sudo apt-get install postgresql-client`

5. **If connection still fails, check environment variable in IDE:**
   - Go to **Run** → **Edit Configurations**
   - Select a Python service (e.g., "05. Portal API")
   - Find Environment variables
   - Verify: `DATABASE_URL=postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay`
   - If different, update it

## NATS Connection Fails

**Symptoms:** Go services log `failed to connect to NATS: context deadline exceeded`

**Solution:**

1. **Check if NATS running:**
   ```bash
   docker ps | grep nats
   ```

2. **If not running, start it:**
   ```bash
   docker run -d \
     -p 4222:4222 \
     -p 8222:8222 \
     --name execrelay-nats \
     nats:latest -js
   ```

3. **Test NATS connection:**
   ```bash
   telnet localhost 4222
   # Should connect successfully
   # Type: exit and press enter
   ```

4. **If connection still fails, check environment variable in IDE:**
   - Go to **Run** → **Edit Configurations**
   - Select a Go service (e.g., "01. Ingress")
   - Find Environment variables
   - Verify: `NATS_URL=nats://localhost:4222`
   - If different, update it

## Logs Show Nothing or Logs Are Truncated

**Symptoms:**
- Console window is blank
- Logs stop mid-flow

**Solution A - Clear console and restart:**
1. In Run window, click the red **X** (clear output)
2. Click Stop button
3. Re-run the service

**Solution B - Increase log buffer:**
1. **Run** → **Edit Configurations**
2. Select a service
3. Find "Console" section
4. Increase "Buffer size" to 50000 (or higher)
5. Apply & restart

**Solution C - Log to file instead:**
1. In IDE Run window, right-click service tab
2. Select **Open in External Console** (shows more output)
3. Or manually redirect when launching:
   ```bash
   # In Run configuration, set "Parameters" field to:
   2>&1 | tee /tmp/service.log
   ```

## Breakpoints Don't Work in Python

**Symptoms:** Set breakpoint, run service, code doesn't pause at breakpoint.

**Solution:**
1. Make sure Python interpreter is properly selected (see "Python Services Won't Start" above)
2. Ensure you're running in **Debug mode**, not Run mode:
   - Use **Ctrl+Shift+D** instead of **Ctrl+Shift+F10**
   - Or click the green **Debug** button (not Run) in top toolbar
3. Check Python version: `python3 --version` (should be 3.10+)

If still not working:
1. **Run** → **Edit Configurations**
2. Select service
3. Scroll to bottom → check "Run with Python debugger" is enabled
4. Apply & run in Debug mode

## Breakpoints Work but Debugger is Slow

**Symptoms:** Debugger pauses at breakpoint but is very slow to step/evaluate.

**Solution:**
- Debuggers add overhead. This is normal in Python.
- Use print/log statements instead for frequently-executed code
- Or focus debugging on specific slow sections only

## Services Keep Crashing/Restarting

**Symptoms:** Services start, run for 10 seconds, then crash repeatedly.

**Check logs in Run window for:**

1. **"Signal terminated"** → Database/NATS disconnected
   - Make sure docker postgres/nats are running
   - Check docker logs: `docker logs execrelay-postgres`

2. **"panic: listen tcp :8080"** → Port already in use
   - Follow "Address already in use" solution above

3. **"FATAL: database does not exist"** → DB not initialized
   - Run:
     ```bash
     psql postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay < infra/postgres/init/001_schema.sql
     psql postgresql://execrelay:execrelay_dev_password@localhost:5432/execrelay < infra/postgres/init/002_advanced_features.sql
     ```

4. **Python: "ModuleNotFoundError"** → Dependencies not installed
   - Follow "Python Services Won't Start" → Solution B

## One Service Crashes, Others Keep Running

**Expected behavior:** Services are independent. If Ingress crashes, Bridge/Risk keep running.

**To restart just one:**
1. In Run window, click the service's tab
2. Click Stop button
3. Click Run button

**To restart all:**
1. Click red Stop button (stops all)
2. Click green Run button (starts all)

## Performance Issues (Services Run Slowly)

**Symptoms:** Even simple operations take 10+ seconds.

**Check:**
1. **CPU usage:** Open Activity Monitor → search "ingress" or "python"
   - If >80% CPU, something is stuck in a loop
   - Check logs for repeated error messages

2. **Memory usage:** Activity Monitor → check memory column
   - Python services can grow unbounded if there's a leak
   - Normal: 100-200 MB each
   - Concern: >500 MB

3. **Disk I/O:** Run `iostat` in Terminal
   - If high, database queries might be slow
   - Check PostgreSQL logs: `docker logs execrelay-postgres`

4. **Network:** Check NATS lag:
   ```bash
   curl http://localhost:8222/jsz | jq '.consumer_info'
   ```
   - Should be <100 events lag
   - If >1000, consumers are stuck

## Debug Flag Not Working (Always Verbose)

**Symptoms:** Changed `DEBUG=false` in config, but logs still verbose.

**Solution:**
1. **Run** → **Edit Configurations**
2. Select service
3. In Environment variables field, find the DEBUG line
4. Make sure it shows: `DEBUG=false` (not `DEBUG=true`)
5. Click **Apply** → **OK**
6. Stop and re-run service

Note: Changes take effect on service restart, not mid-run.

## Configuration XML Corruption

**Symptoms:** IDE crashes when opening Run Configurations dialog.

**Solution:**
1. Close GoLand/IntelliJ
2. Backup configs:
   ```bash
   cp -r ~/.idea/runConfigurations ~/.idea/runConfigurations.backup
   # OR (for newer IDE versions using .run/):
   cp -r .run .run.backup
   ```
3. Delete corrupt config files
4. Restore from project `.run/` directory (should auto-sync)
5. Reopen GoLand/IntelliJ

## Still Stuck?

1. **Check full logs:**
   - Each service's console output contains the actual error
   - Search for "ERROR" or "panic" or "Traceback"

2. **Check related guides:**
   - `GOLAND_IDE_SETUP.md` — full setup guide
   - `STANDALONE_DEPLOYMENT.md` — environment setup
   - `DEBUG_LOGGING.md` — logging reference

3. **Verify prerequisites are running:**
   ```bash
   docker ps
   # Should show: postgres + nats containers
   
   ps aux | grep -E "go|python"
   # Should NOT show old processes (indicates clean state)
   ```

4. **Reset to clean state:**
   ```bash
   # Stop all services
   pkill -f "python3 apps"
   pkill -f "go run"
   pkill ingress bridge dxtrade
   
   # Stop containers
   docker stop execrelay-postgres execrelay-nats
   docker rm execrelay-postgres execrelay-nats
   
   # Restart them
   docker run -d -e POSTGRES_PASSWORD=execrelay_dev_password -e POSTGRES_DB=execrelay -p 5432:5432 postgres:14
   docker run -d -p 4222:4222 -p 8222:8222 nats:latest -js
   
   # In IDE: close all configs, invalidate cache, reopen
   ```

5. **Contact / Report:**
   - If issue persists, check system logs: `dmesg` (last 20 lines)
   - IDE crash logs: `~/Library/Logs/JetBrains/*` (macOS)
