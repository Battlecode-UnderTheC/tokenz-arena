// See sandbox.h. Semantics follow unswbc 1.2.7 sandbox.py / turn.py line by line.
#include "sandbox.h"

#include <pthread.h>

#include <algorithm>
#include <chrono>
#include <cstring>
#include <map>
#include <stdexcept>
#include <thread>
#include <atomic>
#include <ctime>

#include "sha256.h"

namespace cj {

// ---------------------------------------------------------------- errno, kinds
enum : int32_t {
  ERR_OK = 0, ERR_E2BIG = 1, ERR_EACCES = 2, ERR_EBADF = 8, ERR_EINVAL = 28, ERR_EISDIR = 31, ERR_ENOENT = 44,
  ERR_ENOSYS = 52, ERR_ENOTDIR = 54, ERR_ENOTSUP = 58, ERR_ENOTTY = 59, ERR_EOVERFLOW = 61, ERR_ERANGE = 68,
  ERR_EROFS = 69, ERR_EEXIST = 20,
};
enum : int { DIR = 3, REGULAR = 4, CHARACTER = 2 };
constexpr uint64_t ALL_RIGHTS = ~0ULL;

// ---------------------------------------------------------------- Rng
Rng::Rng(const std::string &key, const std::string &process) {
  std::string seed = key + std::string(1, '\0') + process;
  sha256::Ctx c;
  c.update(seed.data(), seed.size());
  uint8_t d[32];
  c.final(d);
  for (int i = 0; i < 4; i++) {
    uint64_t v = 0;
    for (int b = 7; b >= 0; b--) v = v << 8 | d[i * 8 + b];
    s[i] = v;
  }
}

static inline uint64_t rotl(uint64_t v, int k) { return (v << k) | (v >> (64 - k)); }

uint64_t Rng::next() {
  uint64_t out = rotl(s[1] * 5, 7) * 9;
  uint64_t t = s[1] << 17;
  s[2] ^= s[0];
  s[3] ^= s[1];
  s[1] ^= s[2];
  s[0] ^= s[3];
  s[2] ^= t;
  s[3] = rotl(s[3], 45);
  return out;
}

std::string Rng::fill(size_t n) {
  std::string out;
  while (out.size() < n) {
    uint64_t v = next();
    out.append((const char *)&v, 8);  // little-endian host
  }
  out.resize(n);
  return out;
}

// ---------------------------------------------------------------- Framer
static const char PARK[] = "\0UNSWBC PARK";
static const size_t PARK_LEN = 12;

void Framer::arm() {
  out.clear();
  line.clear();
  armed = true;
  done = false;
  park_at.reset();
}

static bool is_space(uint8_t c) { return c == ' ' || (c >= 9 && c <= 13); }

bool Framer::feed(const uint8_t *p, size_t n) {
  for (size_t i = 0; i < n; i++) {
    uint8_t byte = p[i];
    if (byte != 0x0A) {
      if (line.size() < BUFFER_LIMIT) line.push_back((char)byte);
      continue;
    }
    std::string l = line;
    if (!l.empty() && l.back() == '\r') l.pop_back();
    line.clear();
    if (l == "READY") continue;
    if (l.size() >= PARK_LEN && memcmp(l.data(), PARK, PARK_LEN) == 0) {
      size_t a = PARK_LEN, b = l.size();
      while (a < b && is_space(l[a])) a++;
      while (b > a && is_space(l[b - 1])) b--;
      bool digits = a < b;
      for (size_t k = a; k < b && digits; k++) digits = l[k] >= '0' && l[k] <= '9';
      int64_t v = -1;
      if (digits && b - a <= 18) v = std::stoll(l.substr(a, b - a));
      park_at = v;  // a count too long to fit never matches what was written
      continue;
    }
    if (!armed) continue;
    if (l == "ENDTURN") {
      armed = false;
      done = true;
      return true;
    }
    if (out.size() < BUFFER_LIMIT) {
      size_t room = BUFFER_LIMIT - out.size();
      out.append(l, 0, std::min(room, l.size()));
      if (out.size() < BUFFER_LIMIT) out.push_back('\n');
    }
  }
  return false;
}

std::string Framer::take() {
  if (out.size() < BUFFER_LIMIT && !done) {
    size_t room = BUFFER_LIMIT - out.size();
    out.append(line, 0, std::min(room, line.size()));
  }
  line.clear();
  std::string r = utf8_replace(out);
  out.clear();
  return r;
}

// bytes.decode("utf-8", "replace").encode(): CPython replaces each maximal
// invalid subpart with U+FFFD.
std::string utf8_replace(const std::string &s) {
  static const char REP[] = "\xEF\xBF\xBD";
  std::string out;
  out.reserve(s.size());
  const uint8_t *p = (const uint8_t *)s.data();
  size_t n = s.size(), i = 0;
  while (i < n) {
    uint8_t b = p[i];
    if (b < 0x80) { out.push_back((char)b); i++; continue; }
    int need;
    uint8_t lo = 0x80, hi = 0xBF;
    if (b >= 0xC2 && b <= 0xDF) need = 1;
    else if (b == 0xE0) { need = 2; lo = 0xA0; }
    else if ((b >= 0xE1 && b <= 0xEC) || b == 0xEE || b == 0xEF) need = 2;
    else if (b == 0xED) { need = 2; hi = 0x9F; }
    else if (b == 0xF0) { need = 3; lo = 0x90; }
    else if (b >= 0xF1 && b <= 0xF3) need = 3;
    else if (b == 0xF4) { need = 3; hi = 0x8F; }
    else { out += REP; i++; continue; }
    size_t k = 1;
    bool ok = true;
    for (; k <= (size_t)need; k++) {
      if (i + k >= n) { ok = false; break; }
      uint8_t c = p[i + k];
      uint8_t l2 = k == 1 ? lo : 0x80, h2 = k == 1 ? hi : 0xBF;
      if (c < l2 || c > h2) { ok = false; break; }
    }
    if (ok) {
      out.append((const char *)p + i, need + 1);
      i += need + 1;
    } else {
      out += REP;
      i += k;  // the lead and the continuation bytes that were valid so far
    }
  }
  return out;
}

// ---------------------------------------------------------------- host table
namespace {

struct Exit { int code; };
struct HostError { std::string msg; };
struct Unsupported { std::string msg; bool listed; };
struct Dead {};

enum Id {
  ARGS_SIZES_GET, ARGS_GET, ENVIRON_SIZES_GET, ENVIRON_GET, CLOCK_TIME_GET, CLOCK_RES_GET,
  RANDOM_GET, SCHED_YIELD, POLL_ONEOFF, FD_PRESTAT_GET, FD_PRESTAT_DIR_NAME, FD_FDSTAT_GET,
  FD_FDSTAT_SET_FLAGS, FDX_FDFLAGS_GET, FDX_FDFLAGS_SET, FD_FILESTAT_GET, PATH_FILESTAT_GET,
  PATH_READLINK, PATHX_OPEN2, FD_CLOSE, PATH_CREATE_DIRECTORY, PATH_UNLINK_FILE, PATH_RENAME,
  PATH_REMOVE_DIRECTORY, PATH_SYMLINK, PATH_LINK, PATH_FILESTAT_SET_TIMES, FD_FILESTAT_SET_TIMES,
  FD_FILESTAT_SET_SIZE, FD_PWRITE, FD_ADVISE, FD_DATASYNC, FD_SYNC, FDX_DUP, CLOCKX_TIME_SET,
  PROCX_PARENT, PROCX_RAISE_INTERVAL, FD_RENUMBER, FDX_DUP2, FD_READ, FD_PREAD, FD_SEEK, FD_TELL,
  FD_READDIR, FD_WRITE, GETCWD, CHDIR, TTY_GET, THREAD_ID, THREAD_PARALLELISM, THREAD_EXIT,
  PROC_ID, PROC_SIGNALS_SIZES_GET, PROC_SIGNALS_GET, PROC_EXIT, PROC_EXIT2, FUTEX_WAIT,
  FUTEX_WAKE, FUTEX_WAKE_ALL,
};

struct Def { const char *key; Id id; int nparams; int nresults; };

const char *W = "wasi_snapshot_preview1.";
const char *X = "wasix_32v1.";

const std::map<std::string, Def> &table() {
  static const std::map<std::string, Def> t = [] {
    std::map<std::string, Def> m;
    auto add = [&](const char *mod, const char *name, Id id, int np, int nr) {
      std::string k = std::string(mod) + name;
      m[k] = Def{nullptr, id, np, nr};
    };
    add(W, "args_sizes_get", ARGS_SIZES_GET, 2, 1);
    add(W, "args_get", ARGS_GET, 2, 1);
    add(W, "environ_sizes_get", ENVIRON_SIZES_GET, 2, 1);
    add(W, "environ_get", ENVIRON_GET, 2, 1);
    add(W, "clock_time_get", CLOCK_TIME_GET, 3, 1);
    add(W, "clock_res_get", CLOCK_RES_GET, 2, 1);
    add(W, "random_get", RANDOM_GET, 2, 1);
    add(W, "sched_yield", SCHED_YIELD, 0, 1);
    add(W, "poll_oneoff", POLL_ONEOFF, 4, 1);
    add(W, "fd_prestat_get", FD_PRESTAT_GET, 2, 1);
    add(W, "fd_prestat_dir_name", FD_PRESTAT_DIR_NAME, 3, 1);
    add(W, "fd_fdstat_get", FD_FDSTAT_GET, 2, 1);
    add(W, "fd_fdstat_set_flags", FD_FDSTAT_SET_FLAGS, 2, 1);
    add(X, "fd_fdflags_get", FDX_FDFLAGS_GET, 2, 1);
    add(X, "fd_fdflags_set", FDX_FDFLAGS_SET, 2, 1);
    add(W, "fd_filestat_get", FD_FILESTAT_GET, 2, 1);
    add(W, "path_filestat_get", PATH_FILESTAT_GET, 5, 1);
    add(W, "path_readlink", PATH_READLINK, 6, 1);
    add(X, "path_open2", PATHX_OPEN2, 10, 1);
    add(W, "fd_close", FD_CLOSE, 1, 1);
    add(W, "path_create_directory", PATH_CREATE_DIRECTORY, 3, 1);
    add(W, "path_unlink_file", PATH_UNLINK_FILE, 3, 1);
    add(W, "path_rename", PATH_RENAME, 6, 1);
    add(W, "path_remove_directory", PATH_REMOVE_DIRECTORY, 3, 1);
    add(W, "path_symlink", PATH_SYMLINK, 5, 1);
    add(W, "path_link", PATH_LINK, 7, 1);
    add(W, "path_filestat_set_times", PATH_FILESTAT_SET_TIMES, 7, 1);
    add(W, "fd_filestat_set_times", FD_FILESTAT_SET_TIMES, 4, 1);
    add(W, "fd_filestat_set_size", FD_FILESTAT_SET_SIZE, 2, 1);
    add(W, "fd_pwrite", FD_PWRITE, 5, 1);
    add(W, "fd_advise", FD_ADVISE, 4, 1);
    add(W, "fd_datasync", FD_DATASYNC, 1, 1);
    add(W, "fd_sync", FD_SYNC, 1, 1);
    add(X, "fd_dup", FDX_DUP, 2, 1);
    add(X, "clock_time_set", CLOCKX_TIME_SET, 2, 1);
    add(X, "proc_parent", PROCX_PARENT, 2, 1);
    add(X, "proc_raise_interval", PROCX_RAISE_INTERVAL, 3, 1);
    add(W, "fd_renumber", FD_RENUMBER, 2, 1);
    add(X, "fd_dup2", FDX_DUP2, 4, 1);
    add(W, "fd_read", FD_READ, 4, 1);
    add(W, "fd_pread", FD_PREAD, 5, 1);
    add(W, "fd_seek", FD_SEEK, 4, 1);
    add(W, "fd_tell", FD_TELL, 2, 1);
    add(W, "fd_readdir", FD_READDIR, 5, 1);
    add(W, "fd_write", FD_WRITE, 4, 1);
    add(X, "getcwd", GETCWD, 2, 1);
    add(X, "chdir", CHDIR, 2, 1);
    add(X, "tty_get", TTY_GET, 1, 1);
    add(X, "thread_id", THREAD_ID, 1, 1);
    add(X, "thread_parallelism", THREAD_PARALLELISM, 1, 1);
    add(X, "thread_exit", THREAD_EXIT, 1, 0);
    add(X, "proc_id", PROC_ID, 1, 1);
    add(X, "proc_signals_sizes_get", PROC_SIGNALS_SIZES_GET, 1, 1);
    add(X, "proc_signals_get", PROC_SIGNALS_GET, 1, 1);
    add(W, "proc_exit", PROC_EXIT, 1, 0);
    add(X, "proc_exit2", PROC_EXIT2, 1, 0);
    add(X, "futex_wait", FUTEX_WAIT, 4, 1);
    add(X, "futex_wake", FUTEX_WAKE, 2, 1);
    add(X, "futex_wake_all", FUTEX_WAKE_ALL, 2, 1);
    return m;
  }();
  return t;
}

// Implemented by the Python sandbox, not by this host: a bot importing one is refused
// (the wrapper then runs the official judge).
const char *PY_ONLY[] = {"wasix_32v1.reflect_signature", "wasix_32v1.call_dynamic"};

const char *DENIED[] = {
    "callback_signal", "dl_invalid_handle", "dlopen", "dlsym", "epoll_create", "epoll_ctl",
    "epoll_wait", "fd_event", "fd_pipe", "proc_exec4", "proc_fork", "proc_join", "proc_raise",
    "proc_signal", "proc_spawn3", "sock_connect", "sock_open", "sock_send", "sock_send_file",
    "sock_send_to", "thread-spawn", "thread_join", "thread_signal", "thread_sleep", "tty_set"};
const char *EXITS[] = {"proc_exit", "proc_exit2", "thread_exit"};

bool in_list(const char *const *list, size_t n, const std::string &name) {
  for (size_t i = 0; i < n; i++)
    if (name == list[i]) return true;
  return false;
}

std::string name_str(const wasm_name_t *n) { return std::string(n->data, n->size); }

wasm_trap_t *dispatch(void *env, wasmtime_caller_t *caller, wasmtime_val_raw_t *args, size_t) {
  HostSpec *spec = (HostSpec *)env;
  Box *box = (Box *)wasmtime_context_get_data(wasmtime_caller_context(caller));
  try {
    if (spec->waits) {
      std::unique_lock<std::mutex> lk(box->mu);
      while (!box->frozen) box->cv.wait(lk);
      if (box->dead) throw Dead{};
    }
    if (!box->first_) box->mark();
    if (spec->id < 0) {
      if (spec->denied) {
        if (spec->nresults) args[0].i32 = ERR_ENOTSUP;
        return nullptr;
      }
      box->unsupported.push_back(spec->key);
      throw Unsupported{spec->key, true};
    }
    int32_t r = box->host(spec->id, args);
    if (spec->nresults) args[0].i32 = r;
    return nullptr;
  } catch (Exit &e) {
    box->pending = Box::EXIT;
    box->pending_code = e.code;
  } catch (Unsupported &e) {
    box->pending = Box::UNSUPPORTED;
    box->pending_msg = e.msg;
  } catch (HostError &e) {
    box->pending = Box::ERROR;
    box->pending_msg = e.msg;
  } catch (Dead &) {
    box->pending = Box::DEAD;
  }
  static const char m[] = "host";
  return wasmtime_trap_new(m, sizeof m - 1);
}

}  // namespace

std::vector<std::string> missing_imports(wasmtime_module_t *module) {
  std::vector<std::string> out;
  wasm_importtype_vec_t imports;
  wasmtime_module_imports(module, &imports);
  for (size_t i = 0; i < imports.size; i++) {
    const wasm_importtype_t *imp = imports.data[i];
    std::string mod = name_str(wasm_importtype_module(imp)), name = name_str(wasm_importtype_name(imp));
    const wasm_externtype_t *et = wasm_importtype_type(imp);
    wasm_externkind_t kind = wasm_externtype_kind(et);
    std::string key = mod + "." + name;
    if (kind == WASM_EXTERN_MEMORY) {
      if (key != "env.memory") out.push_back(key + " (a memory that is not env.memory)");
      continue;
    }
    if (kind != WASM_EXTERN_FUNC) { out.push_back(key + " (not a function)"); continue; }
    if (in_list(PY_ONLY, 2, key)) { out.push_back(key); continue; }
    auto it = table().find(key);
    if (it != table().end()) {
      const wasm_functype_t *ft = wasm_externtype_as_functype_const(et);
      const wasm_valtype_vec_t *ps = wasm_functype_params(ft), *rs = wasm_functype_results(ft);
      bool ok = (int)ps->size == it->second.nparams && (int)rs->size == it->second.nresults;
      for (size_t k = 0; ok && k < ps->size; k++) {
        wasm_valkind_t vk = wasm_valtype_kind(ps->data[k]);
        ok = vk == WASM_I32 || vk == WASM_I64;
      }
      for (size_t k = 0; ok && k < rs->size; k++) ok = wasm_valtype_kind(rs->data[k]) == WASM_I32;
      if (!ok) out.push_back(key + " (unexpected signature)");
    }
  }
  wasm_importtype_vec_delete(&imports);
  return out;
}

void setup_team(Team &team, wasm_engine_t *engine, wasmtime_module_t *module, const std::string &key) {
  team.key = key;
  team.engine = engine;
  team.module = module;
  team.linker = wasmtime_linker_new(engine);
  wasm_importtype_vec_t imports;
  wasmtime_module_imports(module, &imports);
  for (size_t i = 0; i < imports.size; i++) {
    const wasm_importtype_t *imp = imports.data[i];
    std::string mod = name_str(wasm_importtype_module(imp)), name = name_str(wasm_importtype_name(imp));
    const wasm_externtype_t *et = wasm_importtype_type(imp);
    wasm_externkind_t kind = wasm_externtype_kind(et);
    if (kind == WASM_EXTERN_MEMORY) {
      // parse_imports: (minimum, maximum or 65536)
      const wasm_memorytype_t *mt = wasm_externtype_as_memorytype_const(et);
      team.mem_min = wasmtime_memorytype_minimum(mt);
      uint64_t mx = 0;
      bool has = wasmtime_memorytype_maximum(mt, &mx);
      team.mem_max = has && mx ? mx : 65536;
      continue;
    }
    if (kind != WASM_EXTERN_FUNC) continue;
    const wasm_functype_t *ft = wasm_externtype_as_functype_const(et);
    auto spec = std::make_unique<HostSpec>();
    spec->key = mod + "." + name;
    auto it = table().find(spec->key);
    spec->id = it == table().end() ? -1 : it->second.id;
    spec->denied = in_list(DENIED, sizeof DENIED / sizeof *DENIED, name);
    spec->waits = !in_list(EXITS, 3, name);
    spec->nresults = wasm_functype_results(ft)->size;
    wasmtime_error_t *err = wasmtime_linker_define_func_unchecked(
        team.linker, mod.data(), mod.size(), name.data(), name.size(), ft, dispatch, spec.get(), nullptr);
    if (err) {
      wasm_name_t msg;
      wasmtime_error_message(err, &msg);
      std::string s(msg.data, msg.size);
      wasm_byte_vec_delete(&msg);
      wasmtime_error_delete(err);
      throw std::runtime_error("defining " + spec->key + ": " + s);
    }
    team.specs.push_back(std::move(spec));
  }
  wasm_importtype_vec_delete(&imports);
}

// ---------------------------------------------------------------- Box
Box::Box(Team *t) : team(t), rng(t->key, "") {
  // Sandbox._fds: fds 3 and 4 are both the root, preopened as "/".
  fds.push_back({3, Node{"/", DIR, 0, true}});
  fds.push_back({4, Node{"/", DIR, 0, true}});
}

Box::~Box() {}

static std::string trap_text(wasm_trap_t *trap, wasmtime_error_t *err) {
  wasm_message_t m;
  if (trap) wasm_trap_message(trap, &m);
  else wasmtime_error_message(err, &m);
  std::string s(m.data, m.size);
  wasm_byte_vec_delete(&m);
  while (!s.empty() && s.back() == '\0') s.pop_back();
  return s;
}

bool RETRY_ON_EXIT = true;
std::atomic<int64_t> STAT_INST_NS{0}, STAT_THREAD_NS{0}, STAT_BOXES{0};

static int64_t thread_ns() {
  timespec ts;
  clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
  return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

void Box::run() {
  int64_t t0 = thread_ns();
  STAT_BOXES++;
  struct Done {
    int64_t t0;
    ~Done() { STAT_THREAD_NS += thread_ns() - t0; }
  } done{t0};
  // Sandbox.__init__ + instantiate()
  store = wasmtime_store_new(team->engine, this, nullptr);
  ctx = wasmtime_store_context(store);
  wasm_memorytype_t *mt = nullptr;
  wasmtime_error_t *err = wasmtime_memorytype_new(
      team->mem_min, true, std::min<uint64_t>(team->mem_max, MAX_MEMORY_PAGES), false, true, 16, &mt);
  bool ok = !err;
  if (ok) {
    err = wasmtime_sharedmemory_new(team->engine, mt, &memory);
    ok = !err;
  }
  if (mt) wasm_memorytype_delete(mt);
  wasmtime_instance_t instance;
  wasmtime_func_t start{};
  if (ok) {
    base = wasmtime_sharedmemory_data(memory);
    wasmtime_linker_t *linker = wasmtime_linker_clone(team->linker);
    wasmtime_extern_t item;
    item.kind = WASMTIME_EXTERN_SHAREDMEMORY;
    item.of.sharedmemory = memory;
    err = wasmtime_linker_define(linker, ctx, "env", 3, "memory", 6, &item);
    wasm_trap_t *trap = nullptr;
    if (!err) err = wasmtime_linker_instantiate(linker, ctx, team->module, &instance, &trap);
    wasmtime_linker_delete(linker);
    ok = !err && !trap;
    if (trap) wasm_trap_delete(trap);
    if (ok) {
      wasmtime_extern_t e;
      if (wasmtime_instance_export_get(ctx, &instance, "wasmer_metering_remaining_points", 32, &e) &&
          e.kind == WASMTIME_EXTERN_GLOBAL) {
        meter = e.of.global;
        has_meter = true;
      }
      if (wasmtime_instance_export_get(ctx, &instance, "wasmer_metering_points_exhausted", 32, &e) &&
          e.kind == WASMTIME_EXTERN_GLOBAL) {
        exhausted = e.of.global;
        has_exhausted = true;
      }
      ok = has_meter && wasmtime_instance_export_get(ctx, &instance, "_start", 6, &e) &&
           e.kind == WASMTIME_EXTERN_FUNC;
      if (ok) start = e.of.func;
    }
  }
  if (err) wasmtime_error_delete(err);
  if (!ok) {
    // _boot fails: the pool offers None, and start() raises "the sandbox failed to start".
    std::lock_guard<std::mutex> lk(mu);
    failure = "the sandbox failed to start";
    exit_code = -1;
    out_closed = true;
    cv.notify_all();
    return;
  }

  STAT_INST_NS += thread_ns() - t0;
  // Sandbox._enter(_start)
  pending = NONE;
  wasm_trap_t *trap = nullptr;
  err = wasmtime_func_call(ctx, &start, nullptr, 0, nullptr, 0, &trap);
  std::string fail_msg;
  std::optional<int> code;
  if (!err && !trap) {
    code = 0;
  } else {
    std::string text = pending == NONE ? trap_text(trap, err) : "";
    if (pending == EXIT) {
      code = pending_code;
    } else if (!unsupported.empty()) {
      fail_msg = "sandbox error: " + unsupported[0];  // _drive: Unsupported
    } else {
      bool burnt = false;
      if (has_exhausted) {
        wasmtime_val_t v;
        wasmtime_global_get(ctx, &exhausted, &v);
        burnt = v.of.i32 != 0;
      }
      fail_msg = burnt ? "exceeded CPU limit"
                       : "sandbox error: " + (pending == NONE ? text : pending_msg);
    }
  }
  if (trap) wasm_trap_delete(trap);
  if (err) wasmtime_error_delete(err);
  spent();
  has_meter = false;
  {
    std::lock_guard<std::mutex> lk(mu);
    if (code) exit_code = code;
    if (!fail_msg.empty()) failure = fail_msg;  // charge_write may have set it already
    out_closed = true;                           // _drive: stdout.close()
    cv.notify_all();
  }
  wasmtime_store_delete(store);
  store = nullptr;
  wasmtime_sharedmemory_delete(memory);
  memory = nullptr;
}

int64_t Box::meter_get() {
  wasmtime_val_t v;
  wasmtime_global_get(ctx, &meter, &v);
  return v.of.i64;
}

void Box::meter_set(int64_t x) {
  wasmtime_val_t v;
  v.kind = WASMTIME_I64;
  v.of.i64 = x;
  wasmtime_error_t *e = wasmtime_global_set(ctx, &meter, &v);
  if (e) wasmtime_error_delete(e);
}

int64_t Box::spent() {
  if (has_meter) {
    int64_t now = meter_get();
    if (first_) first_ = false;
    else spent_ += last_ - now;
    last_ = now;
  }
  return spent_;
}

void Box::end_turn() {
  ended = true;
  frozen = false;  // caller holds mu
}

uint64_t Box::data_len() { return memory ? wasmtime_sharedmemory_data_size(memory) : 0; }

void Box::mark() {
  if (ended) return;
  reported_ = spent();
  std::lock_guard<std::mutex> lk(mu);
  live = {reported_ - turn_, data_len()};
}

void Box::refill(int64_t points) {
  if (!has_meter) return;
  int64_t gap = spent() - reported_;
  turn_ = reported_;
  ended = false;
  int64_t left = std::max<int64_t>(0, points - gap);
  meter_set(left);
  last_ = left;
}

__int128 Box::now() { return (__int128)spent() + slept_ns; }

void Box::charge(int64_t points) {
  if (!has_meter) return;
  int64_t left = meter_get();
  meter_set(std::max<int64_t>(0, left - points));
}

void Box::charge_write(__int128 total) {
  if (!has_meter) return;
  __int128 cost = WRITE_SYSCALL_COST + total * WRITE_BYTE_COST;
  int64_t left = meter_get();
  __int128 nv = (__int128)left - cost;
  meter_set(nv < INT64_MIN ? INT64_MIN : (int64_t)nv);
  if (!first_) mark();
  if ((__int128)left < cost) {
    if (has_exhausted) {
      wasmtime_val_t v;
      v.kind = WASMTIME_I32;
      v.of.i32 = 1;
      wasmtime_error_t *e = wasmtime_global_set(ctx, &exhausted, &v);
      if (e) wasmtime_error_delete(e);
    }
    {
      std::lock_guard<std::mutex> lk(mu);
      failure = "exceeded CPU limit";
    }
    throw Exit{137};
  }
}

void Box::bound(int64_t ptr, int64_t n) {
  uint64_t len = data_len();
  if (ptr < 0 || n < 0 || (uint64_t)(ptr + n) > len)
    throw HostError{"guest pointer " + std::to_string(ptr) + "+" + std::to_string(n) + " outside " +
                    std::to_string(len) + " bytes"};
}

void Box::wr(int64_t ptr, const void *data, size_t n) {
  bound(ptr, (int64_t)n);
  if (n) memcpy(base + ptr, data, n);
}

std::string Box::rd(int64_t ptr, size_t n) {
  bound(ptr, (int64_t)n);
  return std::string((const char *)base + ptr, n);
}

Box::Node *Box::node(int fd) {
  for (auto &p : fds)
    if (p.first == fd) return &p.second;
  return nullptr;
}

int Box::free_fd() {
  int fd = 3;
  while (node(fd)) fd++;
  return fd;
}

static void put_u64(std::string &s, size_t at, uint64_t v) { memcpy(&s[at], &v, 8); }
static void put_u32(std::string &s, size_t at, uint32_t v) { memcpy(&s[at], &v, 4); }

// Sandbox._resolve: the root is an empty directory, so only "/" exists.
static bool resolves_to_root(const std::string &base, const std::string &rel) {
  std::string v = !rel.empty() && rel[0] == '/' ? rel : base + "/" + rel;
  std::vector<std::string> parts;
  size_t i = 0;
  while (i <= v.size()) {
    size_t j = v.find('/', i);
    if (j == std::string::npos) j = v.size();
    std::string part = v.substr(i, j - i);
    if (part == "" || part == ".") {
    } else if (part == "..") {
      if (!parts.empty()) parts.pop_back();
    } else {
      parts.push_back(part);
    }
    i = j + 1;
  }
  return parts.empty();
}

int32_t Box::filestat_root(int64_t out) {
  // _filestat of the pool's empty temp directory. The judge reports a real
  // inode and size here; this host reports 0 and 4096 (see README).
  std::string s(64, '\0');
  put_u64(s, 0, 1);
  put_u64(s, 8, 0);
  s[16] = (char)DIR;
  put_u64(s, 24, 1);
  put_u64(s, 32, 4096);
  wr(out, s.data(), s.size());
  return ERR_OK;
}

int32_t Box::read_stdin(int32_t iovs, int32_t n, int32_t out) {
  int64_t total = 0;
  for (int32_t k = 0; k < n; k++) {
    std::string iov = rd((int64_t)iovs + 8LL * k, 8);
    uint32_t ptr, length;
    memcpy(&ptr, iov.data(), 4);
    memcpy(&length, iov.data() + 4, 4);
    std::string chunk;
    std::optional<int64_t> refill_with;
    {
      // _Pipe.read
      std::unique_lock<std::mutex> lk(mu);
      bool parked = false;
      while (in_off >= in.size() && !in_closed) {
        if (!parked) {
          parked = true;
          parks++;
          cv.notify_all();
          // Boot is over at the first park: the pool's start() names the worker now.
        }
        cv.wait(lk);
      }
      size_t avail = in.size() - in_off;
      size_t take = std::min<size_t>(avail, length);
      chunk.assign(in, in_off, take);
      in_off += take;
      if (in_off == in.size()) { in.clear(); in_off = 0; }
      if (budget) { refill_with = budget; budget.reset(); }
    }
    if (refill_with) refill(*refill_with);
    if (!chunk.empty()) wr(ptr, chunk.data(), chunk.size());
    total += (int64_t)chunk.size();
    if (chunk.size() < length) break;
  }
  charge(total * READ_BYTE_COST);
  u32(out, (uint32_t)total);
  return ERR_OK;
}

int32_t Box::fd_write(int32_t fd, int32_t iovs, int32_t n, int32_t out) {
  std::vector<std::pair<uint32_t, uint32_t>> chunks;
  __int128 total = 0;
  for (int32_t k = 0; k < n; k++) {
    std::string iov = rd((int64_t)iovs + 8LL * k, 8);
    uint32_t ptr, length;
    memcpy(&ptr, iov.data(), 4);
    memcpy(&length, iov.data() + 4, 4);
    chunks.push_back({ptr, length});
    total += length;
  }
  charge_write(total);
  for (auto &c : chunks) {
    bound(c.first, c.second);
    const uint8_t *p = base + c.first;
    std::lock_guard<std::mutex> lk(mu);
    if (fd == 2) {
      err.append((const char *)p, c.second);
    } else if (framer) {
      // _Pipe.feed
      if (framer->feed(p, c.second)) end_turn();
      cv.notify_all();
    }
  }
  u32(out, (uint32_t)total);
  return ERR_OK;
}

int32_t Box::host(int id, wasmtime_val_raw_t *a) {
  auto I = [&](int k) -> int64_t { return a[k].i32; };
  auto L = [&](int k) -> int64_t { return a[k].i64; };
  switch (id) {
    case ARGS_SIZES_GET:
      u32(I(0), 1);
      u32(I(1), 4);  // "bot\0"
      return ERR_OK;
    case ARGS_GET:
      u32(I(0), (uint32_t)I(1));
      wr(I(1), "bot", 4);
      return ERR_OK;
    case ENVIRON_SIZES_GET:
      u32(I(0), 1);
      u32(I(1), 10);  // "TERM=dumb\0"
      return ERR_OK;
    case ENVIRON_GET:
      u32(I(0), (uint32_t)I(1));
      wr(I(1), "TERM=dumb", 10);
      return ERR_OK;
    case CLOCK_TIME_GET: {
      __int128 t = (I(0) == 0 ? (__int128)VIRTUAL_EPOCH_NS : 0) + now();
      u64(I(2), (uint64_t)t);
      return ERR_OK;
    }
    case CLOCK_RES_GET:
      u64(I(1), 1);
      return ERR_OK;
    case RANDOM_GET: {
      int64_t n = I(1);
      std::string bytes = n > 0 ? rng.fill((size_t)n) : std::string();
      wr(I(0), bytes.data(), bytes.size());
      return ERR_OK;
    }
    case SCHED_YIELD:
      return ERR_OK;
    case POLL_ONEOFF: {
      int64_t subs = I(0), events = I(1), n = I(2), out = I(3);
      if (n == 0) return ERR_EINVAL;
      for (int64_t k = 0; k < n; k++)
        if ((uint8_t)rd(subs + 48 * k + 8, 1)[0] != 0) return ERR_E2BIG;
      std::optional<__int128> wait;
      std::vector<uint64_t> fired;
      for (int64_t k = 0; k < n; k++) {
        int64_t b = subs + 48 * k;
        uint64_t userdata;
        memcpy(&userdata, rd(b, 8).data(), 8);
        rd(b + 8, 1);
        std::string c = rd(b + 16, 26);
        uint64_t timeout;
        uint16_t flags;
        memcpy(&timeout, c.data() + 8, 8);
        memcpy(&flags, c.data() + 24, 2);
        __int128 nanos = timeout;
        if (flags & 1) {
          __int128 d = (__int128)timeout - now();
          nanos = d > 0 ? d : 0;
        }
        wait = wait ? std::min(*wait, nanos) : nanos;
        fired.push_back(userdata);
      }
      if (wait && *wait) slept_ns += *wait;
      for (size_t k = 0; k < fired.size(); k++) {
        // struct.pack("<QHBxQH6x", userdata, 0, 0, 0, 0): 28 bytes
        std::string e(28, '\0');
        put_u64(e, 0, fired[k]);
        wr(events + 32 * (int64_t)k, e.data(), e.size());
      }
      u32(out, (uint32_t)fired.size());
      return ERR_OK;
    }
    case FD_PRESTAT_GET: {
      Node *nd = node((int)I(0));
      if (!nd || !nd->preopen) return ERR_EBADF;
      std::string s(8, '\0');
      put_u32(s, 4, 1);
      wr(I(1), s.data(), 8);
      return ERR_OK;
    }
    case FD_PRESTAT_DIR_NAME: {
      Node *nd = node((int)I(0));
      if (!nd || !nd->preopen) return ERR_EBADF;
      wr(I(1), "/", I(2) >= 1 ? 1 : 0);
      return ERR_OK;
    }
    case FD_FDSTAT_GET: {
      int64_t fd = I(0);
      int kind;
      if (fd >= 0 && fd <= 2) kind = CHARACTER;
      else {
        Node *nd = node((int)fd);
        if (!nd) return ERR_EBADF;
        kind = nd->kind;
      }
      std::string s(24, '\0');
      s[0] = (char)kind;
      put_u64(s, 8, ALL_RIGHTS);
      put_u64(s, 16, ALL_RIGHTS);
      wr(I(1), s.data(), 24);
      return ERR_OK;
    }
    case FD_FDSTAT_SET_FLAGS:
    case FDX_FDFLAGS_SET:
    case CHDIR:
      return ERR_OK;
    case FDX_FDFLAGS_GET:
      u32(I(1), 0);
      return ERR_OK;
    case FD_FILESTAT_GET: {
      int64_t fd = I(0);
      if (fd >= 0 && fd <= 2) {
        std::string s(64, '\0');
        put_u64(s, 0, 1);
        put_u64(s, 8, (uint64_t)fd);
        s[16] = (char)CHARACTER;
        put_u64(s, 24, 1);
        wr(I(1), s.data(), 64);
        return ERR_OK;
      }
      Node *nd = node((int)fd);
      return nd ? filestat_root(I(1)) : ERR_EBADF;
    }
    case PATH_FILESTAT_GET: {
      Node *nd = node((int)I(0));
      if (!nd) return ERR_EBADF;
      std::string p = rd(I(2), (size_t)std::max<int64_t>(0, I(3)));
      if (!resolves_to_root(nd->vpath, p)) return ERR_ENOENT;
      return filestat_root(I(4));
    }
    case PATH_READLINK: {
      Node *nd = node((int)I(0));
      if (!nd) return ERR_EBADF;
      std::string p = rd(I(1), (size_t)std::max<int64_t>(0, I(2)));
      return resolves_to_root(nd->vpath, p) ? ERR_EINVAL : ERR_ENOENT;
    }
    case PATHX_OPEN2: {
      Node *nd = node((int)I(0));
      if (!nd) return ERR_EBADF;
      std::string p = rd(I(2), (size_t)std::max<int64_t>(0, I(3)));
      int64_t oflags = I(4);
      if ((oflags & 1) || (oflags & 8)) return ERR_EACCES;  // nothing is writable
      if (!resolves_to_root(nd->vpath, p)) return ERR_ENOENT;
      int fd = free_fd();
      fds.push_back({fd, Node{"/", DIR, 0, false}});
      std::sort(fds.begin(), fds.end(), [](auto &x, auto &y) { return x.first < y.first; });
      u32(I(9), (uint32_t)fd);
      return ERR_OK;
    }
    case FD_CLOSE: {
      int fd = (int)I(0);
      fds.erase(std::remove_if(fds.begin(), fds.end(), [&](auto &p) { return p.first == fd; }), fds.end());
      return ERR_OK;
    }
    case PATH_CREATE_DIRECTORY:
    case PATH_UNLINK_FILE: {
      Node *nd = node((int)I(0));
      if (!nd) return ERR_EBADF;
      rd(I(1), (size_t)std::max<int64_t>(0, I(2)));
      return ERR_EACCES;
    }
    case PATH_RENAME: {
      if (!node((int)I(0)) || !node((int)I(3))) return ERR_EBADF;
      rd(I(1), (size_t)std::max<int64_t>(0, I(2)));
      rd(I(4), (size_t)std::max<int64_t>(0, I(5)));
      return ERR_EACCES;
    }
    case PATH_REMOVE_DIRECTORY:
    case PATH_FILESTAT_SET_TIMES: {
      // _mutable(fd, ptr, n): ENOENT before EACCES
      int64_t fd = I(0), ptr = id == PATH_REMOVE_DIRECTORY ? I(1) : I(2),
              n = id == PATH_REMOVE_DIRECTORY ? I(2) : I(3);
      Node *nd = node((int)fd);
      if (!nd) return ERR_EBADF;
      std::string p = rd(ptr, (size_t)std::max<int64_t>(0, n));
      if (!resolves_to_root(nd->vpath, p)) return ERR_ENOENT;
      return ERR_EACCES;
    }
    case PATH_SYMLINK: {
      Node *nd = node((int)I(2));
      if (!nd) return ERR_EBADF;
      rd(I(3), (size_t)std::max<int64_t>(0, I(4)));
      return ERR_EACCES;
    }
    case PATH_LINK: {
      Node *nd = node((int)I(4));
      if (!nd) return ERR_EBADF;
      rd(I(5), (size_t)std::max<int64_t>(0, I(6)));
      return ERR_EACCES;
    }
    case FD_FILESTAT_SET_TIMES:
    case FD_FILESTAT_SET_SIZE:
      return node((int)I(0)) ? ERR_EACCES : ERR_EBADF;
    case FD_PWRITE:
      return fd_write((int32_t)I(0), (int32_t)I(1), (int32_t)I(2), (int32_t)I(4));
    case FD_ADVISE:
    case FD_DATASYNC:
    case FD_SYNC:
      return node((int)I(0)) ? ERR_OK : ERR_EBADF;
    case FDX_DUP: {
      Node *nd = node((int)I(0));
      if (!nd) return ERR_EBADF;
      int fd = free_fd();
      Node copy = *nd;  // the judge shares the node; only a directory's pos could differ
      fds.push_back({fd, copy});
      std::sort(fds.begin(), fds.end(), [](auto &x, auto &y) { return x.first < y.first; });
      u32(I(1), (uint32_t)fd);
      return ERR_OK;
    }
    case CLOCKX_TIME_SET:
    case PROCX_PARENT:
    case PROCX_RAISE_INTERVAL:
      return ERR_ENOSYS;
    case FD_RENUMBER: {
      int a0 = (int)I(0), b0 = (int)I(1);
      Node *nd = node(a0);
      if (nd) {
        Node copy = *nd;
        fds.erase(std::remove_if(fds.begin(), fds.end(), [&](auto &p) { return p.first == a0 || p.first == b0; }),
                  fds.end());
        fds.push_back({b0, copy});
        std::sort(fds.begin(), fds.end(), [](auto &x, auto &y) { return x.first < y.first; });
      }
      return ERR_OK;
    }
    case FDX_DUP2: {
      int a0 = (int)I(0), b0 = (int)I(2);
      Node *nd = node(a0);
      if (nd && a0 != b0) {
        Node copy = *nd;
        fds.erase(std::remove_if(fds.begin(), fds.end(), [&](auto &p) { return p.first == b0; }), fds.end());
        fds.push_back({b0, copy});
        std::sort(fds.begin(), fds.end(), [](auto &x, auto &y) { return x.first < y.first; });
      }
      u32(I(3), (uint32_t)I(2));
      return ERR_OK;
    }
    case FD_READ: {
      int64_t fd = I(0);
      if (fd == 0) return read_stdin((int32_t)I(1), (int32_t)I(2), (int32_t)I(3));
      Node *nd = node((int)fd);
      if (!nd) return ERR_EBADF;
      return ERR_EISDIR;  // every node here is a directory
    }
    case FD_PREAD: {
      // _read_node on a directory: every iov reads nothing
      Node *nd = node((int)I(0));
      if (!nd) return ERR_EBADF;
      for (int32_t k = 0; k < (int32_t)I(2); k++) {
        std::string iov = rd(I(1) + 8LL * k, 8);
        uint32_t length;
        memcpy(&length, iov.data() + 4, 4);
        if (length > 0) break;
      }
      charge(0);
      u32(I(4), 0);
      return ERR_OK;
    }
    case FD_SEEK: {
      Node *nd = node((int)I(0));
      if (!nd) return ERR_EBADF;
      int64_t whence = I(2), off = L(1);
      int64_t basev;
      if (whence == 0 || whence == -3) basev = 0;
      else if (whence == 1 || whence == -2) basev = (int64_t)nd->pos;
      else if (whence == 2 || whence == -1) basev = 0;  // size of a directory node
      else throw HostError{"tuple index out of range"};
      __int128 p = (__int128)basev + off;
      nd->pos = p > 0 ? (uint64_t)p : 0;
      u64(I(3), nd->pos);
      return ERR_OK;
    }
    case FD_TELL: {
      Node *nd = node((int)I(0));
      if (!nd) return ERR_EBADF;
      u64(I(1), nd->pos);
      return ERR_OK;
    }
    case FD_READDIR: {
      Node *nd = node((int)I(0));
      if (!nd) return ERR_EBADF;
      if (nd->kind != DIR) return ERR_ENOTDIR;
      int64_t buf = I(1), buflen = I(2), cookie = L(3);
      const char *names[2] = {".", ".."};
      std::string blob;
      for (int index = 0; index < 2; index++) {
        if (index < cookie) continue;
        std::string e(24, '\0');
        put_u64(e, 0, (uint64_t)index + 1);
        put_u64(e, 8, (uint64_t)index + 7);
        put_u32(e, 16, (uint32_t)strlen(names[index]));
        e[20] = (char)DIR;
        blob += e + names[index];
        if ((int64_t)blob.size() >= buflen) break;
      }
      // blob[:buflen], with Python's negative slice bounds
      int64_t cut = buflen >= 0 ? std::min<int64_t>(buflen, blob.size())
                                : std::max<int64_t>(0, (int64_t)blob.size() + buflen);
      wr(buf, blob.data(), (size_t)cut);
      u32(I(4), (uint32_t)std::min<int64_t>((int64_t)blob.size(), buflen));
      return ERR_OK;
    }
    case FD_WRITE:
      return fd_write((int32_t)I(0), (int32_t)I(1), (int32_t)I(2), (int32_t)I(3));
    case GETCWD: {
      int64_t ptr = I(0), lenptr = I(1);
      uint32_t maxlen = ru32(lenptr);
      u32(lenptr, 1);
      if (1 > maxlen) return ERR_ERANGE;
      if (ptr == 0 || maxlen == 0) return ERR_EINVAL;
      wr(ptr, "/", 1);
      if (1 < maxlen) wr(ptr + 1, "\0", 1);
      return ERR_OK;
    }
    case TTY_GET:
      return ERR_ENOTTY;
    case THREAD_ID:
    case THREAD_PARALLELISM:
    case PROC_ID:
      u32(I(0), 1);
      return ERR_OK;
    case PROC_SIGNALS_SIZES_GET:
      u32(I(0), 0);
      return ERR_OK;
    case PROC_SIGNALS_GET:
      return ERR_OK;
    case THREAD_EXIT:
    case PROC_EXIT:
    case PROC_EXIT2:
      throw Exit{(int)I(0)};
    case FUTEX_WAIT: {
      int64_t ptr = I(0), expected = I(1), tptr = I(2), woken = I(3);
      if ((int64_t)ru32(ptr) != expected) {
        u8(woken, 1);
        return ERR_OK;
      }
      if (tptr && rd(tptr, 1)[0]) {
        u8(woken, 0);
        return ERR_OK;
      }
      throw Unsupported{"futex_wait would block forever (the judge runs one thread)", false};
    }
    case FUTEX_WAKE:
    case FUTEX_WAKE_ALL:
      u8(I(1), 0);
      return ERR_OK;
  }
  throw HostError{"unhandled host function"};
}

// ---------------------------------------------------------------- Dragon (SandboxBot)

static std::string payload(const std::string &block, bool activate) {
  bool ends = block.size() >= 2 && block[block.size() - 1] == '\n' && block[block.size() - 2] == '\n';
  return (activate ? "R" : "") + block + (ends ? "" : "\n");
}

void Dragon::start() {
  stop();
  auto b = std::make_shared<Box>(pool);
  // A worker thread with the stack Python's threads get (8 MiB), so wasmtime's
  // 512 KiB wasm stack limit is what bounds recursion, as in the judge.
  pthread_attr_t attr;
  pthread_attr_init(&attr);
  pthread_attr_setstacksize(&attr, 8u << 20);
  pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
  auto *hold = new std::shared_ptr<Box>(b);
  pthread_t th;
  int rc = pthread_create(&th, &attr, [](void *p) -> void * {
    auto *h = (std::shared_ptr<Box> *)p;
    (*h)->run();
    delete h;
    return nullptr;
  }, hold);
  pthread_attr_destroy(&attr);
  if (rc != 0) {
    delete hold;
    throw std::runtime_error("the sandbox failed to start");
  }
  // WasmPool keeps spares booted up to their first stdin read with Rng(key, "");
  // start() takes one and renames its Rng. Boot this one to the same point.
  std::unique_lock<std::mutex> lk(b->mu);
  auto until = std::chrono::steady_clock::now() + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                                                     std::chrono::duration<double>(WALL_LIMIT_S));
  while (b->parks == 0 && !b->out_closed)
    if (b->cv.wait_until(lk, until) == std::cv_status::timeout) break;
  if (b->out_closed && b->failure == "the sandbox failed to start")
    throw std::runtime_error("the sandbox failed to start");
  b->rng = Rng(pool->key, name);
  b->framer = &framer;
  activate = false;  // a wasm spare is never "activated"
  box = b;
}

void Dragon::stop() {
  if (box) {
    std::lock_guard<std::mutex> lk(box->mu);
    err_acc += box->err;
    box->framer = nullptr;
    box->frozen = true;
    box->in_closed = true;
    box->dead = true;
    box->cv.notify_all();
  }
  box.reset();
}

void Dragon::fresh() {
  start();
  written = 0;
  is_new = true;
}

bool Dragon::write(const std::string &data) {
  std::lock_guard<std::mutex> lk(box->mu);
  if (box->out_closed) return false;
  box->budget = MAX_TURN_POINTS;
  parks_seen = box->parks;
  box->in += data;
  box->frozen = true;
  box->cv.notify_all();
  return true;
}

Dragon::State Dragon::poll(double timeout) {
  std::unique_lock<std::mutex> lk(box->mu);
  if (framer.done) return DATA;
  if (box->out_closed) return EXITED;
  if (box->parks > parks_seen) return PARK;
  if (timeout > 0) box->cv.wait_for(lk, std::chrono::duration<double>(timeout));
  return WAIT;
}

std::string Dragon::reason() {
  if (!box) return "exited";
  std::lock_guard<std::mutex> lk(box->mu);
  if (!box->failure.empty()) return box->failure;
  return "exited with code " + std::to_string(box->exit_code.value_or(0));
}

std::string Dragon::skip(const std::string &r) {
  error = r;
  stop();
  return "";
}

std::string Dragon::ask(const std::string &block) {
  using clock = std::chrono::steady_clock;
  error.reset();
  if (!box) fresh();
  for (int attempt = 0; attempt < 2; attempt++) {
    {
      std::lock_guard<std::mutex> lk(box->mu);
      framer.arm();
    }
    std::string data = payload((is_new ? init : std::string()) + block, activate);
    if (!write(data)) {
      if (attempt == 0) {
        fresh();
        continue;
      }
      return skip(reason());
    }
    written += (int64_t)data.size();
    activate = is_new = false;
    auto deadline = clock::now() + std::chrono::duration_cast<clock::duration>(
                                       std::chrono::duration<double>(WALL_LIMIT_S));
    while (true) {
      double left = std::chrono::duration<double>(deadline - clock::now()).count();
      State st = poll(std::max(0.0, left));
      bool finished;
      {
        std::lock_guard<std::mutex> lk(box->mu);
        finished = framer.done || (framer.park_at && *framer.park_at == written) || st == PARK;
        if (finished) return framer.take();
      }
      if (st == EXITED) break;
      if (clock::now() >= deadline) {
        stop();
        return skip("ran out of time");
      }
    }
    std::string r = reason();
    bool said;
    {
      std::lock_guard<std::mutex> lk(box->mu);
      said = !framer.out.empty() || !framer.line.empty();
    }
    if (attempt || said || !RETRY_ON_EXIT) return skip(r);   // --no-retry: dies in its turn, as on the server
    fresh();
  }
  return "";
}

std::pair<int64_t, uint64_t> Dragon::live() {
  if (!box) return {0, 0};
  std::lock_guard<std::mutex> lk(box->mu);
  return box->live;
}

std::string Dragon::take_stderr() {
  if (box) {
    std::lock_guard<std::mutex> lk(box->mu);
    err_acc += box->err;
    box->err.clear();
  }
  std::string out;
  out.swap(err_acc);
  return out;
}

}  // namespace cj
