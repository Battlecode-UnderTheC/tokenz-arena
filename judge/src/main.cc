// cppjudge: `unswbc run MAP A B --sandbox` for wasm (C/C++) bots, without Python.
// A port of unswbc 1.2.7 run.py execute() and engine.py EngineModule; the bots' sandbox
// is in sandbox.cc. The bots must already be built and metered (the `unswbc-cpp`
// wrapper does that with unswbc's own Python and passes --wasm-a/--wasm-b).
#include <sys/stat.h>
#include <unistd.h>

#include <wasi.h>
#include <wasmtime.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdarg>
#include <cerrno>
#include <functional>
#include <cstring>
#include <ctime>
#include <fstream>
#include <map>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "sandbox.h"
#include "sha256.h"

using namespace cj;

namespace {

constexpr int MAX_ROUNDS = 500;
constexpr int DEBUG_ALL = 15, DEBUG_LIMITS = 16;
constexpr size_t REPLY_CAP_UNUSED = 1 << 16;

// ---------------------------------------------------------------- output
std::string OUT;
void flush_out() {
  if (!OUT.empty()) {
    fwrite(OUT.data(), 1, OUT.size(), stdout);
    OUT.clear();
  }
  fflush(stdout);
}
void say(const std::string &line) {
  OUT += line;
  OUT += '\n';
  if (OUT.size() > (1 << 16)) flush_out();
}

[[noreturn]] void die(const std::string &msg, int code = 1) {
  flush_out();
  fprintf(stderr, "error: %s\n", msg.c_str());
  fflush(stderr);
  _exit(code);
}

std::string fmt(const char *f, ...) {
  char buf[512];
  va_list ap;
  va_start(ap, f);
  vsnprintf(buf, sizeof buf, f, ap);
  va_end(ap);
  return buf;
}

std::string read_file(const std::string &path) {
  std::ifstream f(path, std::ios::binary);
  if (!f) throw std::runtime_error("cannot read " + path);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

bool is_file(const std::string &p) {
  struct stat st;
  return stat(p.c_str(), &st) == 0 && S_ISREG(st.st_mode);
}
bool is_dir(const std::string &p) {
  struct stat st;
  return stat(p.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
}
void mkdirs(const std::string &p) {
  for (size_t i = 1; i <= p.size(); i++)
    if (i == p.size() || p[i] == '/') mkdir(p.substr(0, i).c_str(), 0777);
}

std::string error_text(wasmtime_error_t *err, wasm_trap_t *trap) {
  wasm_message_t m;
  if (trap) wasm_trap_message(trap, &m);
  else wasmtime_error_message(err, &m);
  std::string s(m.data, m.size);
  wasm_byte_vec_delete(&m);
  while (!s.empty() && s.back() == '\0') s.pop_back();
  return s;
}

void check(wasmtime_error_t *err, const std::string &what) {
  if (err) {
    std::string s = error_text(err, nullptr);
    wasmtime_error_delete(err);
    throw std::runtime_error(what + ": " + s);
  }
}

// ---------------------------------------------------------------- python-ish paths
std::string path_norm(std::string p) {
  // pathlib.Path(p) as str: collapse "//", drop "." parts and a trailing "/".
  bool abs = !p.empty() && p[0] == '/';
  std::vector<std::string> parts;
  size_t i = 0;
  while (i <= p.size()) {
    size_t j = p.find('/', i);
    if (j == std::string::npos) j = p.size();
    std::string part = p.substr(i, j - i);
    if (!part.empty() && part != ".") parts.push_back(part);
    i = j + 1;
  }
  std::string out = abs ? "/" : "";
  for (size_t k = 0; k < parts.size(); k++) out += (k ? "/" : "") + parts[k];
  return out.empty() ? "." : out;
}
std::string path_name(const std::string &p) {
  std::string n = path_norm(p);
  if (n == "." || n == "/") return "";
  size_t s = n.rfind('/');
  return s == std::string::npos ? n : n.substr(s + 1);
}
std::string path_suffix(const std::string &p) {
  std::string n = path_name(p);
  size_t i = n.rfind('.');
  if (i != std::string::npos && i > 0 && i < n.size() - 1) return n.substr(i);
  return "";
}
std::string path_stem(const std::string &p) {
  std::string n = path_name(p), s = path_suffix(p);
  return n.substr(0, n.size() - s.size());
}
std::string join(const std::string &a, const std::string &b) {
  std::string n = path_norm(a);
  return n == "." ? b : (n == "/" ? "/" + b : n + "/" + b);
}

std::string label(const std::string &arg) {
  std::string name = path_name(arg);
  if (name == "" || name == "." || name == "..") {
    char buf[4096];
    std::string cwd = getcwd(buf, sizeof buf) ? buf : "";
    if (path_norm(arg) == ".." || (arg.size() >= 2 && arg.substr(arg.size() - 2) == ".."))
      cwd = cwd.substr(0, cwd.rfind('/'));
    name = path_name(cwd);
  }
  return name.empty() ? "bot" : name;
}

// ---------------------------------------------------------------- run.py helpers
std::string elapsed(double s) {
  if (s < 60) return fmt("%.1fs", s);
  return fmt("%dm%02ds", (int)s / 60, (int)s % 60);
}
std::string round_ms(double s) { return s < 1 ? fmt("%dms", (int)(s * 1000)) : fmt("%.1fs", s); }
std::string points(int64_t v) {
  if (v >= 100000) return fmt("%.1fM", v / 1e6);
  std::string d = std::to_string(v), out;
  int c = 0;
  for (int i = (int)d.size() - 1; i >= 0; i--) {
    out.insert(out.begin(), d[i]);
    if (++c % 3 == 0 && i > 0 && d[i - 1] != '-') out.insert(out.begin(), ',');
  }
  return out;
}
int64_t percentile(const std::vector<int64_t> &o, int q) {
  int64_t n = (int64_t)o.size();
  int64_t idx = (q * n + 99) / 100 - 1;
  return o[std::max<int64_t>(0, idx)];
}

struct MatchResult {
  int rounds, winner_code, end_reason, a_dragons, b_dragons, a_length, b_length, events, a_queen,
      b_queen, a_longest, b_longest;
};

std::string verdict(const MatchResult &r) {
  if (r.end_reason == 0) return r.winner_code ? "by elimination" : "both teams eliminated";
  int a[3] = {r.a_queen, r.a_longest, r.a_length}, b[3] = {r.b_queen, r.b_longest, r.b_length};
  if (r.winner_code == 0)
    return fmt("equal length: queen %d, longest %d, total %d each", a[0], a[1], a[2]);
  int *won = r.winner_code == 1 ? a : b, *lost = r.winner_code == 1 ? b : a;
  const char *labels[3] = {"longer queen", "longest dragon", "total length"};
  for (int i = 0; i < 3; i++)
    if (won[i] != lost[i]) return fmt("%s, %d to %d", labels[i], won[i], lost[i]);
  return "on length";
}

const char *death_reason(char c) {
  switch (c) {
    case 'W': return "hit a wall";
    case 'S': return "hit itself";
    case 'O': return "hit another dragon";
    case 'H': return "lost a head-to-head";
    case 'A': return "no valid action";
  }
  return "died";
}

double mono() {
  return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

struct Progress {
  bool verbose;
  int round = 0;
  double start, round_start, last = 0;
  explicit Progress(bool v) : verbose(v) { start = round_start = mono(); }
  void update(int round_num, int a, int b) {
    if (round_num == round) return;
    double now = mono();
    last = now - round_start;
    round_start = now;
    round = round_num;
    double eta = round_num ? (now - start) / round_num * (MAX_ROUNDS - round_num) : 0.0;
    std::string text = fmt("running round %d/%d --  last: %s  eta: %s  dragons: %d vs %d", round_num + 1,
                           MAX_ROUNDS, round_ms(last).c_str(), elapsed(eta).c_str(), a, b);
    if (verbose || round_num == 1 || round_num % 50 == 0 || round_num == MAX_ROUNDS) {
      say(text);
      flush_out();
    }
  }
};

// ---------------------------------------------------------------- module cache
std::string cache_root() {
  const char *x = getenv("XDG_CACHE_HOME");
  std::string base = x && *x ? x : std::string(getenv("HOME") ? getenv("HOME") : "/tmp") + "/.cache";
  return base + "/unswbc/cppjudge";
}

wasmtime_module_t *load_module(wasm_engine_t *engine, const std::string &path, const std::string &tag) {
  std::string blob = read_file(path);
  std::string key = sha256::hex(blob.data(), blob.size()).substr(0, 32);
  std::string dir = cache_root();
  std::string cached = dir + "/" + tag + "-" + key + "-wasmtime49.cwasm";
  wasmtime_module_t *m = nullptr;
  if (is_file(cached)) {
    wasmtime_error_t *e = wasmtime_module_deserialize_file(engine, cached.c_str(), &m);
    if (!e) return m;
    wasmtime_error_delete(e);
    m = nullptr;
  }
  check(wasmtime_module_new(engine, (const uint8_t *)blob.data(), blob.size(), &m), "compiling " + path);
  wasm_byte_vec_t ser;
  if (!wasmtime_module_serialize(m, &ser)) {
    mkdirs(dir);
    std::string tmp = cached + "." + std::to_string(getpid());
    FILE *f = fopen(tmp.c_str(), "wb");
    if (f) {
      fwrite(ser.data, 1, ser.size, f);
      fclose(f);
      rename(tmp.c_str(), cached.c_str());
    }
    wasm_byte_vec_delete(&ser);
  }
  return m;
}

// ---------------------------------------------------------------- engine.py
struct Engine {
  wasm_engine_t *engine;
  wasmtime_module_t *module;
  wasmtime_store_t *store = nullptr;
  wasmtime_context_t *ctx = nullptr;
  wasmtime_instance_t instance;
  wasmtime_memory_t memory;
  std::string pending_err;
  std::function<std::string(int, const std::string &)> on_reply;
  std::function<void(int, int, char)> on_death;
  std::function<void(int, const std::string &)> on_spawn;
  std::function<void(const std::string &)> on_notice;
  std::string long_reply;
  bool has_long = false;

  uint8_t *mem() { return wasmtime_memory_data(ctx, &memory); }

  wasmtime_func_t export_func(const char *name) {
    wasmtime_extern_t e;
    if (!wasmtime_instance_export_get(ctx, &instance, name, strlen(name), &e) || e.kind != WASMTIME_EXTERN_FUNC)
      throw std::runtime_error(std::string("engine has no export ") + name);
    return e.of.func;
  }

  std::vector<wasmtime_val_t> call(const char *name, std::vector<wasmtime_val_t> args, size_t nres) {
    wasmtime_func_t f = export_func(name);
    std::vector<wasmtime_val_t> res(nres);
    wasm_trap_t *trap = nullptr;
    wasmtime_error_t *err = wasmtime_func_call(ctx, &f, args.data(), args.size(), res.data(), nres, &trap);
    if (err || trap) {
      std::string s = error_text(err, trap);
      if (err) wasmtime_error_delete(err);
      if (trap) wasm_trap_delete(trap);
      throw std::runtime_error(s);
    }
    return res;
  }

  static wasmtime_val_t i32(int32_t v) {
    wasmtime_val_t x;
    x.kind = WASMTIME_I32;
    x.of.i32 = v;
    return x;
  }
  static wasmtime_val_t i64(int64_t v) {
    wasmtime_val_t x;
    x.kind = WASMTIME_I64;
    x.of.i64 = v;
    return x;
  }

  static ptrdiff_t stderr_cb(void *data, const unsigned char *p, size_t n) {
    Engine *e = (Engine *)data;
    e->pending_err.append((const char *)p, n);
    size_t at;
    while ((at = e->pending_err.find('\n')) != std::string::npos) {
      std::string line = e->pending_err.substr(0, at);
      e->pending_err.erase(0, at + 1);
      e->on_notice(utf8_replace(line));
    }
    return (ptrdiff_t)n;
  }

  static wasm_trap_t *reply_cb(void *env, wasmtime_caller_t *, wasmtime_val_raw_t *a, size_t) {
    Engine *e = (Engine *)env;
    int32_t dragon = a[0].i32, ptr = a[1].i32, length = a[2].i32, out = a[3].i32, cap = a[4].i32;
    std::string reply;
    if (e->has_long) {
      reply.swap(e->long_reply);
      e->has_long = false;
    } else {
      std::string block((const char *)e->mem() + (uint32_t)ptr, (size_t)(uint32_t)length);
      reply = e->on_reply(dragon, block);
    }
    if ((int64_t)reply.size() > cap) {
      e->long_reply = reply;
      e->has_long = true;
      a[0].i32 = (int32_t)reply.size();
      return nullptr;
    }
    memcpy(e->mem() + (uint32_t)out, reply.data(), reply.size());
    a[0].i32 = (int32_t)reply.size();
    return nullptr;
  }
  static wasm_trap_t *log_cb(void *env, wasmtime_caller_t *, wasmtime_val_raw_t *a, size_t) {
    Engine *e = (Engine *)env;
    e->on_death(a[0].i32, a[1].i32, (char)a[2].i32);
    return nullptr;
  }
  static wasm_trap_t *spawn_cb(void *env, wasmtime_caller_t *, wasmtime_val_raw_t *a, size_t) {
    Engine *e = (Engine *)env;
    std::string init((const char *)e->mem() + (uint32_t)a[1].i32, (size_t)(uint32_t)a[2].i32);
    e->on_spawn(a[0].i32, init);
    return nullptr;
  }

  // EngineModule.run
  MatchResult run(const std::string &map, int debug, uint64_t seed) {
    store = wasmtime_store_new(engine, nullptr, nullptr);
    ctx = wasmtime_store_context(store);
    wasi_config_t *wasi = wasi_config_new();
    wasi_config_set_stderr_custom(wasi, stderr_cb, this, nullptr);
    check(wasmtime_context_set_wasi(ctx, wasi), "wasi");
    wasmtime_linker_t *linker = wasmtime_linker_new(engine);
    check(wasmtime_linker_define_wasi(linker), "wasi");
    auto ft = [](size_t np, size_t nr) {
      wasm_valtype_vec_t p, r;
      wasm_valtype_vec_new_uninitialized(&p, np);
      for (size_t i = 0; i < np; i++) p.data[i] = wasm_valtype_new(WASM_I32);
      wasm_valtype_vec_new_uninitialized(&r, nr);
      for (size_t i = 0; i < nr; i++) r.data[i] = wasm_valtype_new(WASM_I32);
      return wasm_functype_new(&p, &r);
    };
    wasm_functype_t *t5 = ft(5, 1), *t3 = ft(3, 0);
    check(wasmtime_linker_define_func_unchecked(linker, "unswbc", 6, "bot_reply", 9, t5, reply_cb, this, nullptr), "link");
    check(wasmtime_linker_define_func_unchecked(linker, "unswbc", 6, "log", 3, t3, log_cb, this, nullptr), "link");
    check(wasmtime_linker_define_func_unchecked(linker, "unswbc", 6, "bot_spawn", 9, t3, spawn_cb, this, nullptr), "link");
    wasm_functype_delete(t5);
    wasm_functype_delete(t3);
    wasm_trap_t *trap = nullptr;
    wasmtime_error_t *err = wasmtime_linker_instantiate(linker, ctx, module, &instance, &trap);
    if (err || trap) throw std::runtime_error("instantiating the engine: " + error_text(err, trap));
    wasmtime_linker_delete(linker);
    wasmtime_extern_t e;
    if (!wasmtime_instance_export_get(ctx, &instance, "memory", 6, &e) || e.kind != WASMTIME_EXTERN_MEMORY)
      throw std::runtime_error("engine has no memory");
    memory = e.of.memory;
    if (wasmtime_instance_export_get(ctx, &instance, "_initialize", 11, &e) && e.kind == WASMTIME_EXTERN_FUNC)
      call("_initialize", {}, 0);

    int32_t map_ptr = call("ubc_alloc", {i32((int32_t)map.size())}, 1)[0].of.i32;
    memcpy(mem() + (uint32_t)map_ptr, map.data(), map.size());
    int32_t out_ptr = call("ubc_alloc", {i32(12 * 4)}, 1)[0].of.i32;
    int32_t code = call("ubc_run", {i32(map_ptr), i32((int32_t)map.size()), i32(debug), i64((int64_t)seed), i32(out_ptr)}, 1)[0].of.i32;
    if (!pending_err.empty()) {
      on_notice(utf8_replace(pending_err));
      pending_err.clear();
    }
    if (code != 0) throw std::runtime_error(read_error());
    int32_t v[12];
    memcpy(v, mem() + (uint32_t)out_ptr, 48);
    return MatchResult{v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9], v[10], v[11]};
  }

  std::string replay(const std::string &a, const std::string &b) {
    std::vector<int32_t> names;
    for (const std::string *n : {&a, &b}) {
      int32_t p = call("ubc_alloc", {i32((int32_t)n->size())}, 1)[0].of.i32;
      memcpy(mem() + (uint32_t)p, n->data(), n->size());
      names.push_back(p);
      names.push_back((int32_t)n->size());
    }
    int32_t size = call("ubc_replay", {i32(names[0]), i32(names[1]), i32(names[2]), i32(names[3])}, 1)[0].of.i32;
    call("ubc_free", {i32(names[0])}, 0);
    call("ubc_free", {i32(names[2])}, 0);
    if (size < 0) throw std::runtime_error(read_error());
    int32_t base = call("ubc_replay_ptr", {}, 1)[0].of.i32;
    return std::string((const char *)mem() + (uint32_t)base, (size_t)size);
  }

  std::string read_error() {
    int32_t p = call("ubc_error", {}, 1)[0].of.i32;
    std::string out((const char *)mem() + (uint32_t)p);
    out = utf8_replace(out);
    return out.empty() ? "engine failed" : out;
  }
};

struct Args {
  std::string map, name_a, name_b, wasm_a, wasm_b, engine, output;
  bool verbose = false, no_replay = false, has_seed = false;
  uint64_t seed = 0;
  int debug = DEBUG_ALL | DEBUG_LIMITS;
};

void usage() {
  fprintf(stderr,
          "usage: cppjudge run MAP NAME_A NAME_B --wasm-a A.wasm --wasm-b B.wasm --engine unswbc_engine.wasm\n"
          "                [--seed S] [-v] [-o OUT] [--no-replay] [--debug N]\n"
          "       cppjudge imports BOT.wasm   (exit 3 and list what this host lacks)\n");
  _exit(2);
}

wasm_engine_t *bot_engine() {
  // sandbox.compile_once
  wasm_config_t *c = wasm_config_new();
  wasmtime_config_wasm_threads_set(c, true);
  wasmtime_config_wasm_bulk_memory_set(c, true);
  wasmtime_config_wasm_simd_set(c, true);
  wasmtime_config_wasm_exceptions_set(c, true);
  wasmtime_config_wasm_wide_arithmetic_set(c, true);
  wasmtime_config_wasm_multi_memory_set(c, true);
  wasmtime_config_wasm_multi_value_set(c, true);
  wasmtime_config_wasm_reference_types_set(c, true);
  wasmtime_config_wasm_tail_call_set(c, true);
  wasmtime_config_shared_memory_set(c, true);
  return wasm_engine_new_with_config(c);
}

int run(const Args &args) {
  if (!is_file(args.map)) die("not found: " + args.map);
  // Before any output: a bot this host can't run exits 3, and the wrapper hands
  // the whole game to the official judge.
  wasm_engine_t *beng = bot_engine();
  std::map<std::string, wasmtime_module_t *> modules;
  const std::string *paths[2] = {&args.wasm_a, &args.wasm_b};
  for (int t = 0; t < 2; t++) {
    wasmtime_module_t *&m = modules[*paths[t]];
    if (!m) m = load_module(beng, *paths[t], "bot");
    std::vector<std::string> missing = missing_imports(m);
    if (!missing.empty()) die("unsupported by cppjudge: " + missing[0], 3);
  }
  say("loaded wasm vs wasm in the judge's sandbox");
  uint64_t seed = args.seed;
  if (!args.has_seed) {
    std::random_device rd;
    seed = (uint64_t)rd() << 32 | rd();
  }
  say(fmt("seed 0x%016llx", (unsigned long long)seed));
  flush_out();
  Team teams[2];
  for (int t = 0; t < 2; t++)
    setup_team(teams[t], beng, modules[*paths[t]], fmt("%016llx-%c", (unsigned long long)seed, t ? 'b' : 'a'));

  Engine engine;
  engine.engine = wasm_engine_new();
  engine.module = load_module(engine.engine, args.engine, "engine");

  bool verbose = args.verbose;
  std::map<int, std::unique_ptr<Dragon>> live;
  std::map<int, std::string> team_of;
  std::map<std::string, std::vector<int64_t>> spent;
  std::vector<std::string> notices;
  Progress progress(verbose);
  double started = progress.start;

  auto who = [&](int id, const std::string &block) {
    std::string first = block.substr(0, block.find('\n'));
    std::istringstream ss(first);
    std::string h0, h1;
    ss >> h0 >> h1;
    std::string r = (h0 == "ROUND" && !h1.empty()) ? h1 : "?";
    auto it = team_of.find(id);
    return "round " + r + ": bot " + std::to_string(id) + " (team " + (it == team_of.end() ? "?" : it->second) + ")";
  };

  engine.on_notice = [&](const std::string &line) {
    notices.push_back(line);
    if (verbose) say(line);
  };
  engine.on_spawn = [&](int id, const std::string &init) {
    std::string team = "A";
    std::istringstream ls(init);
    std::string line;
    while (std::getline(ls, line)) {
      if (line.rfind("TEAM", 0) == 0) {
        std::istringstream ws(line);
        std::string w0, w1;
        ws >> w0 >> w1;
        team = w1;
        break;
      }
    }
    team_of[id] = team;
    live[id] = std::make_unique<Dragon>(&teams[team == "B" ? 1 : 0], init, std::to_string(id));
  };
  engine.on_reply = [&](int id, const std::string &block) -> std::string {
    std::string first = block.substr(0, block.find('\n'));
    std::istringstream ss(first);
    std::string h0, h1;
    ss >> h0 >> h1;
    if (h0 == "ROUND" && !h1.empty()) {
      int alive[2] = {0, 0};
      for (auto &kv : live) {
        auto it = team_of.find(kv.first);
        alive[(it == team_of.end() ? "A" : it->second) == "B"]++;
      }
      progress.update(std::stoi(h1), alive[0], alive[1]);
    }
    Dragon &bot = *live.at(id);
    std::string out = bot.ask(block);
    if (bot.error) say(who(id, block) + " " + *bot.error);
    if (verbose && !out.empty()) say(who(id, block) + " stdout:\n" + out);
    auto metrics = bot.live();
    if (metrics.first) {
      auto it = team_of.find(id);
      spent[it == team_of.end() ? "?" : it->second].push_back(metrics.first);
      if (verbose)
        say(who(id, block) + " points " + std::to_string(metrics.first) + " memory " + std::to_string(metrics.second));
    }
    if (verbose) {
      std::string noise = bot.take_stderr();
      if (!noise.empty()) say(who(id, block) + " stderr:\n" + utf8_replace(noise));
    }
    return out;
  };
  engine.on_death = [&](int id, int round, char reason) {
    auto it = team_of.find(id);
    say("round " + std::to_string(round) + ": bot " + std::to_string(id) + " (team " +
        (it == team_of.end() ? "?" : it->second) + ") died: " + death_reason(reason));
    auto b = live.find(id);
    if (b != live.end()) {
      b->second->stop();
      live.erase(b);
    }
  };

  std::string map_bytes = read_file(args.map);
  MatchResult result;
  try {
    result = engine.run(map_bytes, args.debug, seed);
  } catch (std::exception &e) {
    for (auto &kv : live) kv.second->stop();
    if (!notices.empty() && !verbose)
      say(fmt("map: %zu defect%s found and corrected -- rerun with -v to see them", notices.size(),
              notices.size() == 1 ? "" : "s"));
    die(args.map + ": " + e.what());
  }
  for (auto &kv : live) kv.second->stop();

  if (!notices.empty() && !verbose)
    say(fmt("map: %zu defect%s found and corrected -- rerun with -v to see them", notices.size(),
            notices.size() == 1 ? "" : "s"));
  int rounds = result.rounds + 1;
  std::string reason = verdict(result);
  std::string took = elapsed(mono() - started);
  if (result.winner_code == 0)
    say(fmt("draw after %d rounds (%s) (%s)", rounds, reason.c_str(), took.c_str()));
  else
    say(fmt("team %c wins after %d rounds (%s) (%s)", result.winner_code == 1 ? 'A' : 'B', rounds,
            reason.c_str(), took.c_str()));
  for (auto &kv : spent) {
    std::vector<int64_t> o = kv.second;
    std::sort(o.begin(), o.end());
    int64_t sum = 0;
    for (int64_t v : o) sum += v;
    int64_t mean = sum >= 0 ? sum / (int64_t)o.size() : -((-sum + (int64_t)o.size() - 1) / (int64_t)o.size());
    say("team " + kv.first + " points per turn: p50 " + points(percentile(o, 50)) + "  p99 " +
        points(percentile(o, 99)) + "  mean " + points(mean) + "  max " + points(o.back()) + "  (" +
        std::to_string(o.size()) + " turns)");
  }

  if (args.no_replay) {
    say("skipped writing replay");
    flush_out();
    return 0;
  }
  std::string path = args.output.empty() ? "replays" : args.output;
  if (is_dir(path) || path_suffix(path).empty()) {
    char stamp[64];
    time_t now = time(nullptr);
    strftime(stamp, sizeof stamp, "%Y-%m-%d-%H%M%S", localtime(&now));
    std::string map_name = path_stem(args.map);
    if (map_name.empty()) map_name = "map";
    std::string la = label(args.name_a), lb = label(args.name_b);
    std::string matchup = la == lb ? la + "-vs-itself" : la + "-vs-" + lb;
    path = join(path, matchup + "-on-" + map_name + "-" + stamp + ".replay");
  }
  path = path_norm(path);
  std::string blob;
  try {
    blob = engine.replay(args.name_a, args.name_b);
  } catch (std::exception &e) {
    die(args.map + ": " + e.what());
  }
  size_t slash = path.rfind('/');
  if (slash != std::string::npos && slash > 0) mkdirs(path.substr(0, slash));
  FILE *f = fopen(path.c_str(), "wb");
  if (!f || fwrite(blob.data(), 1, blob.size(), f) != blob.size())
    die("cannot write the replay to " + path + ": " + strerror(errno));
  fclose(f);
  say("wrote replay: " + path);
  flush_out();
  return 0;
}

}  // namespace

int main(int argc, char **argv) {
  std::vector<std::string> a(argv + 1, argv + argc);
  if (a.size() == 2 && a[0] == "imports") {
    wasm_engine_t *beng = bot_engine();
    try {
      wasmtime_module_t *m = load_module(beng, a[1], "bot");
      auto missing = missing_imports(m);
      for (auto &s : missing) printf("%s\n", s.c_str());
      fflush(stdout);
      _exit(missing.empty() ? 0 : 3);
    } catch (std::exception &e) {
      die(e.what());
    }
  }
  if (a.empty() || a[0] != "run") usage();
  Args args;
  std::vector<std::string> pos;
  for (size_t i = 1; i < a.size(); i++) {
    const std::string &s = a[i];
    auto val = [&]() -> std::string {
      if (i + 1 >= a.size()) usage();
      return a[++i];
    };
    if (s == "-v" || s == "--verbose") args.verbose = true;
    else if (s == "-o" || s == "--output" || s == "--replay") args.output = val();
    else if (s == "--no-replay") args.no_replay = true;
    else if (s == "--seed") {
      std::string v = val();
      try {
        size_t used = 0;
        args.seed = std::stoull(v, &used, 0);
        if (used != v.size()) throw std::invalid_argument(v);
      } catch (...) {
        die("not a number: " + v, 2);
      }
      args.has_seed = true;
    } else if (s == "--wasm-a") args.wasm_a = val();
    else if (s == "--wasm-b") args.wasm_b = val();
    else if (s == "--engine") args.engine = val();
    else if (s == "--debug") args.debug = std::stoi(val());
    else if (s == "--sandbox") {
    } else pos.push_back(s);
  }
  if (pos.size() != 3 || args.wasm_a.empty() || args.wasm_b.empty() || args.engine.empty()) usage();
  args.map = pos[0];
  args.name_a = pos[1];
  args.name_b = pos[2];
  if (!args.output.empty() && args.no_replay) die("--no-replay cannot be used with -o");
  int code;
  try {
    code = run(args);
  } catch (std::exception &e) {
    die(e.what());
  }
  flush_out();
  fflush(stderr);
  // Workers of dead dragons may still be unwinding; the judge's are daemon threads too.
  _exit(code);
}
