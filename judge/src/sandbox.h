// The judge's sandbox for wasm (C/C++) bots: a port of unswbc 1.2.7 sandbox.py
// (Sandbox, WasmPool, SandboxBot, _Pipe, _drive, Rng) and turn.py (Framer, TurnBot)
// to C++ on the wasmtime C API. Each comment names the Python it mirrors.
#pragma once

#include <wasmtime.h>

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace cj {

constexpr int64_t MAX_TURN_POINTS = 100'000'000;
constexpr uint32_t MAX_MEMORY_PAGES = 768;
constexpr uint64_t VIRTUAL_EPOCH_NS = 1'767'225'600'000'000'000ULL;
constexpr int64_t WRITE_SYSCALL_COST = 2'500'000;
constexpr int64_t WRITE_BYTE_COST = 4'000;
constexpr int64_t READ_BYTE_COST = 6;
constexpr int64_t INITIAL_POINTS = INT64_MAX;  // metering.INITIAL_POINTS
constexpr size_t BUFFER_LIMIT = 10 * 1024;      // turn.BUFFER_LIMIT
constexpr double WALL_LIMIT_S = 10.0;           // turn.WALL_LIMIT_S

// sandbox.Rng: xoshiro256**, seeded from sha256(key + "\0" + process).
struct Rng {
  uint64_t s[4];
  Rng(const std::string &key = "", const std::string &process = "");
  uint64_t next();
  std::string fill(size_t n);
};

// turn.Framer. Guarded by the owning Box's mutex.
struct Framer {
  bool armed = false, done = false;
  std::optional<int64_t> park_at;  // None, or -1 for a malformed count
  std::string out, line;
  void arm();
  bool feed(const uint8_t *p, size_t n);  // true when ENDTURN ends the turn
  std::string take();
};

// One compiled bot and the template linker its workers are instantiated from.
struct Team;

// sandbox.Sandbox for a wasm bot, plus its two _Pipes and the frozen Event.
struct Box : std::enable_shared_from_this<Box> {
  Team *team;
  // --- shared with the controller, under mu ---
  std::mutex mu;
  std::condition_variable cv;
  std::string in;            // stdin pipe buffer
  size_t in_off = 0;
  bool in_closed = false;
  uint64_t parks = 0;
  bool out_closed = false;   // stdout pipe closed: the worker's _start has returned
  Framer *framer = nullptr;  // _Pipe.framer
  bool frozen = true;        // sandbox.frozen (Event set)
  bool dead = false;         // stopped by its dragon: nobody will read it again
  std::optional<int64_t> budget;
  std::pair<int64_t, uint64_t> live{0, 0};
  std::optional<int> exit_code;
  std::string failure;
  std::string err;           // the guest's stderr
  Rng rng;
  // --- the worker thread's own ---
  wasmtime_store_t *store = nullptr;
  wasmtime_context_t *ctx = nullptr;
  wasmtime_sharedmemory_t *memory = nullptr;
  uint8_t *base = nullptr;
  wasmtime_global_t meter{}, exhausted{};
  bool has_meter = false, has_exhausted = false;
  int64_t spent_ = 0, last_ = INITIAL_POINTS, turn_ = 0, reported_ = 0;
  __int128 slept_ns = 0;
  bool first_ = true, ended = false;
  std::vector<std::string> unsupported;
  enum Pending { NONE, EXIT, UNSUPPORTED, ERROR, DEAD } pending = NONE;
  int pending_code = 0;
  std::string pending_msg;
  struct Node { std::string vpath; int kind; uint64_t pos = 0; bool preopen = false; };
  std::vector<std::pair<int, Node>> fds;  // fd table (sorted by fd)

  explicit Box(Team *t);
  ~Box();
  void run();  // the worker thread: instantiate, _start, then close stdout

  // sandbox.Sandbox methods, called on the worker thread
  int64_t meter_get();
  void meter_set(int64_t v);
  int64_t spent();
  void mark();
  void refill(int64_t points);
  __int128 now();
  void charge(int64_t points);
  void charge_write(__int128 total);
  void end_turn();
  uint64_t data_len();
  void bound(int64_t ptr, int64_t n);
  void wr(int64_t ptr, const void *data, size_t n);
  std::string rd(int64_t ptr, size_t n);
  void u8(int64_t p, uint8_t v) { wr(p, &v, 1); }
  void u32(int64_t p, uint32_t v) { wr(p, &v, 4); }
  void u64(int64_t p, uint64_t v) { wr(p, &v, 8); }
  uint32_t ru32(int64_t p) { uint32_t v; std::string s = rd(p, 4); memcpy(&v, s.data(), 4); return v; }
  Node *node(int fd);
  int free_fd();
  int32_t host(int id, wasmtime_val_raw_t *a);
  int32_t read_stdin(int32_t iovs, int32_t n, int32_t out);
  int32_t fd_write(int32_t fd, int32_t iovs, int32_t n, int32_t out);
  int32_t filestat_root(int64_t out);
};

struct HostSpec {
  std::string key;  // "module.name"
  int id;           // -1: not implemented here
  bool waits, denied;
  size_t nresults;
};

struct Team {
  std::string key;     // f"{seed:016x}-{team}"
  wasm_engine_t *engine;
  wasmtime_module_t *module;
  wasmtime_linker_t *linker;  // every function import defined; memory per worker
  uint64_t mem_min = 1, mem_max = 65536;
  std::vector<std::unique_ptr<HostSpec>> specs;
};

// Imports of a module that the Python sandbox implements but this host does not.
std::vector<std::string> missing_imports(wasmtime_module_t *module);
void setup_team(Team &team, wasm_engine_t *engine, wasmtime_module_t *module, const std::string &key);

// sandbox.SandboxBot (a turn.TurnBot): one dragon, whose worker is replaced when it dies.
struct Dragon {
  Team *pool;
  std::string init, name;
  Framer framer;
  bool activate = false, is_new = false;
  int64_t written = 0;
  uint64_t parks_seen = 0;
  std::optional<std::string> error;
  std::shared_ptr<Box> box;
  std::string err_acc;

  Dragon(Team *p, std::string i, std::string n) : pool(p), init(std::move(i)), name(std::move(n)) {}
  std::string ask(const std::string &block);
  void start();
  void stop();
  std::pair<int64_t, uint64_t> live();
  std::string take_stderr();

 private:
  void fresh();
  bool write(const std::string &data);
  enum State { DATA, EXITED, PARK, WAIT };
  State poll(double timeout);
  std::string skip(const std::string &reason);
  std::string reason();
};

std::string utf8_replace(const std::string &s);
// unswbc asks a bot again on a fresh worker when it died in its turn before saying anything;
// --no-retry (arena/strictjudge.py) lets it die instead, as the competition server does.
extern bool RETRY_ON_EXIT;
extern std::atomic<int64_t> STAT_INST_NS, STAT_THREAD_NS, STAT_BOXES;

}  // namespace cj
