/*
 * hevcmerge: merge the slices of an H.265 picture into one slice without re-encoding.
 *
 * Hikvision's tiled cameras (DS-2CD2347G1-L, DS-2CD2347G2-LU main stream) send each picture as 3 tiles,
 * one slice per tile. Reolink's mp4 writer (phone app, PC client, NVR backup) stores each GOP in at most
 * 600 pieces, 2 per NAL unit, so 3 slices allow only 100 pictures per GOP. One slice that holds the 3
 * tiles allows 300.
 *
 * Why this is lossless:
 * - the slices of one picture carry the same slice header apart from the slice address;
 * - every tile starts with freshly initialised CABAC contexts and arithmetic decoder, in a separate
 *   slice as well as inside one slice, and prediction never crosses a tile boundary either way;
 * - in-loop filtering across the old slice boundaries depends on loop_filter_across_tiles_enabled_flag
 *   only after the merge, so the merge is done only when that gives the same result.
 * What changes in the bitstream: the merged slice header gets entry points, and each tile except the last
 * one now ends with end_of_slice_segment_flag = 0 and end_of_subset_one_bit = 1 instead of
 * end_of_slice_segment_flag = 1. That is one more terminate bin before the arithmetic coder flushes, which
 * changes the last bits of the tile. Computing them needs the coder's range at the end of the tile, so
 * every tile is entropy-decoded (all syntax elements; no pixels, motion vectors or transforms).
 *
 * Supported: Main/Main 10 profile, 4:2:0, tiles without WPP, no dependent slice segments, no PPS
 * extensions. Anything else, or any doubt, gives a negative result and the caller sends the picture as it
 * came.
 *
 * Syntax and semantics: ITU-T H.265 (v4+) 7.3, 9.3. Context tables follow the specification (the same
 * values as FFmpeg's libavcodec/hevc/cabac.c).
 */
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>

/* glibc 2.34 moved the pthread calls into libc under new symbol versions. Bind to the original ones, so
 * that the library needs no newer glibc than the Python that loads it (python-build-standalone: 2.17). */
#if defined(__GLIBC__) && defined(__aarch64__)
__asm__(".symver pthread_create,pthread_create@GLIBC_2.17");
__asm__(".symver pthread_join,pthread_join@GLIBC_2.17");
#endif

#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API __attribute__((visibility("default")))
#endif

enum {
    E_FEW = 1,      /* fewer than 2 slices: nothing to do */
    E_NOMEM,
    E_NOPARAM,      /* SPS/PPS not seen yet */
    E_SPS,          /* unsupported SPS */
    E_PPS,          /* unsupported PPS (no tiles, WPP, extensions) */
    E_HEADER,       /* slice header could not be parsed or is unsupported */
    E_MISMATCH,     /* slices differ in their headers */
    E_LAYOUT,       /* slices do not cover whole tiles, in order */
    E_FILTER,       /* merging would change in-loop filtering */
    E_CABAC,        /* slice data did not decode cleanly */
    E_TAIL,         /* tile end could not be rewritten */
    E_VERIFY,       /* merged slice did not decode */
    E_SPACE,        /* output buffer too small */
};

/* ------------------------------------------------------------------------------------------------ */
/* bit reader for parameter sets and slice headers                                                   */

typedef struct {
    const uint8_t *b;
    size_t nbits, pos;
    int err;
} BR;

static uint32_t rd(BR *r, int n)
{
    uint32_t v = 0;
    while (n-- > 0) {
        int bit = 0;
        if (r->pos < r->nbits)
            bit = (r->b[r->pos >> 3] >> (7 - (r->pos & 7))) & 1;
        else
            r->err = 1;
        v = (v << 1) | (uint32_t)bit;
        r->pos++;
    }
    return v;
}

static uint32_t ue(BR *r)
{
    int lz = 0;
    while (!rd(r, 1)) {
        if (r->err || ++lz > 31) {
            r->err = 1;
            return 0;
        }
    }
    if (!lz)
        return 0;
    return ((1u << lz) - 1) + rd(r, lz);
}

static int32_t se(BR *r)
{
    uint32_t k = ue(r);
    return (k & 1) ? (int32_t)((k + 1) >> 1) : -(int32_t)(k >> 1);
}

static int ceil_log2(uint32_t v)
{
    int n = 0;
    while ((1u << n) < v)
        n++;
    return n;
}

/* ------------------------------------------------------------------------------------------------ */
/* parameter sets                                                                                   */

#define MAX_SPS 16
#define MAX_PPS 64
#define MAX_TILE_COLS 64
#define MAX_TILE_ROWS 64

typedef struct {
    int valid, gen;
    int chroma_format_idc, width, height, bit_depth, bit_depth_c;
    int log2_max_poc_lsb, log2_min_cb, log2_ctb, log2_min_tb, log2_max_tb;
    int max_th_inter, max_th_intra, amp, sao;
    int pcm, pcm_bd, pcm_bdc, pcm_log2_min, pcm_log2_max;
    int num_st_rps;
    uint8_t st_ndelta[65], st_nused[65];
    int lt_present, num_lt_sps;
    uint8_t lt_used[33];
    int tmvp;
    int ctb_w, ctb_h, min_cb_w, min_cb_h;
    uint8_t *raw;
    size_t raw_len;
} SPS;

typedef struct {
    int valid, gen;
    int sps_id, dep_slices, output_flag, extra_bits, sign_hiding, cabac_init_present;
    int nref_def[2], init_qp, transform_skip, cu_qp_delta, diff_qp_depth;
    int slice_chroma_qp_offsets, weighted_pred, weighted_bipred, transquant_bypass;
    int tiles, wpp, ncols, nrows, uniform, lf_tiles, lf_slices;
    uint16_t colw[MAX_TILE_COLS], rowh[MAX_TILE_ROWS];
    int deblock_override, deblock_disabled, lists_mod, hdr_ext;
    uint8_t *raw;
    size_t raw_len;
} PPS;

static void skip_ptl(BR *r, int max_sub, int *profile, uint32_t *compat)
{
    int i, sp[8] = {0}, sl[8] = {0};
    rd(r, 3);
    *profile = (int)rd(r, 5);
    *compat = rd(r, 32);
    rd(r, 24);
    rd(r, 24);
    rd(r, 8); /* general_level_idc */
    for (i = 0; i < max_sub; i++) {
        sp[i] = (int)rd(r, 1);
        sl[i] = (int)rd(r, 1);
    }
    if (max_sub > 0)
        for (i = max_sub; i < 8; i++)
            rd(r, 2);
    for (i = 0; i < max_sub; i++) {
        if (sp[i]) {
            rd(r, 32);
            rd(r, 32);
            rd(r, 24);
        }
        if (sl[i])
            rd(r, 8);
    }
}

static void skip_scaling_list(BR *r)
{
    int size, matrix, i;
    for (size = 0; size < 4; size++)
        for (matrix = 0; matrix < 6; matrix += (size == 3) ? 3 : 1) {
            if (!rd(r, 1)) {
                ue(r);
            } else {
                int n = 1 << (4 + (size << 1));
                if (n > 64)
                    n = 64;
                if (size > 1)
                    se(r);
                for (i = 0; i < n && !r->err; i++)
                    se(r);
            }
        }
}

/* st_ref_pic_set(idx): counts of delta POCs and of pictures used by the current picture */
static int st_rps(BR *r, int idx, int num, uint8_t *nd, uint8_t *nu)
{
    int inter = idx ? (int)rd(r, 1) : 0;
    int i;
    if (inter) {
        int delta_idx = 1, ref, cnt = 0, used = 0;
        if (idx == num)
            delta_idx = (int)ue(r) + 1;
        if (delta_idx > idx)
            return -1;
        rd(r, 1);
        ue(r);
        ref = idx - delta_idx;
        for (i = 0; i <= nd[ref]; i++) {
            int u = (int)rd(r, 1), d = 1;
            if (!u)
                d = (int)rd(r, 1);
            if (u || d)
                cnt++;
            used += u;
        }
        if (cnt > 32)
            return -1;
        nd[idx] = (uint8_t)cnt;
        nu[idx] = (uint8_t)used;
    } else {
        uint32_t neg = ue(r), pos = ue(r);
        int used = 0;
        if (neg > 16 || pos > 16)
            return -1;
        for (i = 0; i < (int)(neg + pos); i++) {
            ue(r);
            used += (int)rd(r, 1);
        }
        nd[idx] = (uint8_t)(neg + pos);
        nu[idx] = (uint8_t)used;
    }
    return r->err ? -1 : 0;
}

static int parse_sps(SPS *s, const uint8_t *rbsp, size_t n)
{
    BR r = {rbsp, n * 8, 0, 0};
    int max_sub, profile, i;
    uint32_t compat, v;

    memset(s, 0, offsetof(SPS, raw));
    rd(&r, 4);
    max_sub = (int)rd(&r, 3);
    rd(&r, 1);
    skip_ptl(&r, max_sub, &profile, &compat);
    if (!(profile == 1 || profile == 2 || (profile == 0 && (compat & 0x60000000u))))
        return -E_SPS;
    ue(&r); /* id, read by the caller */
    s->chroma_format_idc = (int)ue(&r);
    if (s->chroma_format_idc != 1)
        return -E_SPS;
    s->width = (int)ue(&r);
    s->height = (int)ue(&r);
    if (rd(&r, 1)) {
        ue(&r); ue(&r); ue(&r); ue(&r);
    }
    s->bit_depth = (int)ue(&r) + 8;
    s->bit_depth_c = (int)ue(&r) + 8;
    s->log2_max_poc_lsb = (int)ue(&r) + 4;
    v = rd(&r, 1);
    for (i = v ? 0 : max_sub; i <= max_sub; i++) {
        ue(&r); ue(&r); ue(&r);
    }
    s->log2_min_cb = (int)ue(&r) + 3;
    s->log2_ctb = s->log2_min_cb + (int)ue(&r);
    s->log2_min_tb = (int)ue(&r) + 2;
    s->log2_max_tb = s->log2_min_tb + (int)ue(&r);
    s->max_th_inter = (int)ue(&r);
    s->max_th_intra = (int)ue(&r);
    if (r.err || s->log2_ctb > 6 || s->log2_ctb < 4 || s->log2_max_tb > 5 || s->log2_min_tb >= s->log2_min_cb ||
        s->log2_max_tb > s->log2_ctb || s->bit_depth > 12 || s->bit_depth_c > 12 || s->log2_max_poc_lsb > 16 ||
        s->width <= 0 || s->height <= 0 || s->width > 16384 || s->height > 16384 ||
        (s->width & ((1 << s->log2_min_cb) - 1)) || (s->height & ((1 << s->log2_min_cb) - 1)))
        return -E_SPS;
    if (rd(&r, 1) && rd(&r, 1))
        skip_scaling_list(&r);
    s->amp = (int)rd(&r, 1);
    s->sao = (int)rd(&r, 1);
    s->pcm = (int)rd(&r, 1);
    if (s->pcm) {
        s->pcm_bd = (int)rd(&r, 4) + 1;
        s->pcm_bdc = (int)rd(&r, 4) + 1;
        s->pcm_log2_min = (int)ue(&r) + 3;
        s->pcm_log2_max = s->pcm_log2_min + (int)ue(&r);
        rd(&r, 1);
    }
    s->num_st_rps = (int)ue(&r);
    if (s->num_st_rps > 64)
        return -E_SPS;
    for (i = 0; i < s->num_st_rps; i++)
        if (st_rps(&r, i, s->num_st_rps, s->st_ndelta, s->st_nused))
            return -E_SPS;
    s->lt_present = (int)rd(&r, 1);
    if (s->lt_present) {
        s->num_lt_sps = (int)ue(&r);
        if (s->num_lt_sps > 32)
            return -E_SPS;
        for (i = 0; i < s->num_lt_sps; i++) {
            rd(&r, s->log2_max_poc_lsb);
            s->lt_used[i] = (uint8_t)rd(&r, 1);
        }
    }
    s->tmvp = (int)rd(&r, 1);
    rd(&r, 1); /* strong_intra_smoothing */
    /* VUI and extensions follow; Main/Main 10 has no extension that changes slice data */
    if (r.err)
        return -E_SPS;
    s->ctb_w = (s->width + (1 << s->log2_ctb) - 1) >> s->log2_ctb;
    s->ctb_h = (s->height + (1 << s->log2_ctb) - 1) >> s->log2_ctb;
    s->min_cb_w = s->width >> s->log2_min_cb;
    s->min_cb_h = s->height >> s->log2_min_cb;
    s->valid = 1;
    return 0;
}

static int parse_pps(PPS *p, const uint8_t *rbsp, size_t n)
{
    BR r = {rbsp, n * 8, 0, 0};
    int i;

    memset(p, 0, offsetof(PPS, raw));
    ue(&r); /* id */
    p->sps_id = (int)ue(&r);
    p->dep_slices = (int)rd(&r, 1);
    p->output_flag = (int)rd(&r, 1);
    p->extra_bits = (int)rd(&r, 3);
    p->sign_hiding = (int)rd(&r, 1);
    p->cabac_init_present = (int)rd(&r, 1);
    p->nref_def[0] = (int)ue(&r) + 1;
    p->nref_def[1] = (int)ue(&r) + 1;
    p->init_qp = 26 + se(&r);
    rd(&r, 1); /* constrained_intra_pred: prediction only */
    p->transform_skip = (int)rd(&r, 1);
    p->cu_qp_delta = (int)rd(&r, 1);
    if (p->cu_qp_delta)
        p->diff_qp_depth = (int)ue(&r);
    se(&r);
    se(&r);
    p->slice_chroma_qp_offsets = (int)rd(&r, 1);
    p->weighted_pred = (int)rd(&r, 1);
    p->weighted_bipred = (int)rd(&r, 1);
    p->transquant_bypass = (int)rd(&r, 1);
    p->tiles = (int)rd(&r, 1);
    p->wpp = (int)rd(&r, 1);
    if (p->sps_id >= MAX_SPS || p->nref_def[0] > 15 || p->nref_def[1] > 15)
        return -E_PPS;
    if (p->tiles) {
        p->ncols = (int)ue(&r) + 1;
        p->nrows = (int)ue(&r) + 1;
        if (p->ncols > MAX_TILE_COLS || p->nrows > MAX_TILE_ROWS)
            return -E_PPS;
        p->uniform = (int)rd(&r, 1);
        if (!p->uniform) {
            for (i = 0; i < p->ncols - 1; i++)
                p->colw[i] = (uint16_t)(ue(&r) + 1);
            for (i = 0; i < p->nrows - 1; i++)
                p->rowh[i] = (uint16_t)(ue(&r) + 1);
        }
        p->lf_tiles = (int)rd(&r, 1);
    }
    p->lf_slices = (int)rd(&r, 1);
    if (rd(&r, 1)) {
        p->deblock_override = (int)rd(&r, 1);
        p->deblock_disabled = (int)rd(&r, 1);
        if (!p->deblock_disabled) {
            se(&r);
            se(&r);
        }
    }
    if (rd(&r, 1))
        skip_scaling_list(&r);
    p->lists_mod = (int)rd(&r, 1);
    ue(&r); /* log2_parallel_merge_level: motion derivation only */
    p->hdr_ext = (int)rd(&r, 1);
    if (rd(&r, 1)) /* pps_extension_present_flag */
        return -E_PPS;
    if (r.err)
        return -E_PPS;
    p->valid = 1;
    return 0;
}

/* ------------------------------------------------------------------------------------------------ */
/* tiles (6.5.1)                                                                                    */

typedef struct {
    int sps_gen, pps_gen, n;
    int *rs2ts, *ts2rs, *tile_id; /* tile_id indexed by ts */
    int cap;
} Tiles;

static int build_tiles(Tiles *t, const SPS *s, const PPS *p)
{
    int W = s->ctb_w, H = s->ctb_h, n = W * H, i, j, rs;
    int colw[MAX_TILE_COLS], rowh[MAX_TILE_ROWS], colbd[MAX_TILE_COLS + 1], rowbd[MAX_TILE_ROWS + 1];
    int nc = p->tiles ? p->ncols : 1, nr = p->tiles ? p->nrows : 1;

    if (t->sps_gen == s->gen && t->pps_gen == p->gen && t->n == n)
        return 0;
    if (n > t->cap) {
        free(t->rs2ts);
        t->rs2ts = malloc(sizeof(int) * 3 * (size_t)n);
        if (!t->rs2ts) {
            t->cap = 0;
            return -E_NOMEM;
        }
        t->cap = n;
    }
    t->ts2rs = t->rs2ts + n;
    t->tile_id = t->rs2ts + 2 * n;
    if (nc > W || nr > H)
        return -E_PPS;
    if (!p->tiles || p->uniform) {
        for (i = 0; i < nc; i++)
            colw[i] = ((i + 1) * W) / nc - (i * W) / nc;
        for (j = 0; j < nr; j++)
            rowh[j] = ((j + 1) * H) / nr - (j * H) / nr;
    } else {
        int sum = 0;
        for (i = 0; i < nc - 1; i++)
            sum += colw[i] = p->colw[i];
        if (sum >= W)
            return -E_PPS;
        colw[nc - 1] = W - sum;
        sum = 0;
        for (j = 0; j < nr - 1; j++)
            sum += rowh[j] = p->rowh[j];
        if (sum >= H)
            return -E_PPS;
        rowh[nr - 1] = H - sum;
    }
    colbd[0] = 0;
    for (i = 0; i < nc; i++)
        colbd[i + 1] = colbd[i] + colw[i];
    rowbd[0] = 0;
    for (j = 0; j < nr; j++)
        rowbd[j + 1] = rowbd[j] + rowh[j];
    for (rs = 0; rs < n; rs++) {
        int x = rs % W, y = rs / W, tx = 0, ty = 0, ts = 0;
        while (x >= colbd[tx + 1])
            tx++;
        while (y >= rowbd[ty + 1])
            ty++;
        for (i = 0; i < tx; i++)
            ts += rowh[ty] * colw[i];
        for (j = 0; j < ty; j++)
            ts += W * rowh[j];
        ts += (y - rowbd[ty]) * colw[tx] + x - colbd[tx];
        t->rs2ts[rs] = ts;
        t->ts2rs[ts] = rs;
        t->tile_id[ts] = ty * nc + tx;
    }
    t->sps_gen = s->gen;
    t->pps_gen = p->gen;
    t->n = n;
    return 0;
}

/* ------------------------------------------------------------------------------------------------ */
/* slice segment header (7.3.6.1)                                                                   */

enum { SLICE_B = 0, SLICE_P = 1, SLICE_I = 2 };

typedef struct {
    int nal_type, first, no_output, pps_id, addr, slice_type;
    int sao_luma, sao_chroma, nref[2], mvd_l1_zero, cabac_init, max_merge, qp, lf_across, deblock_disabled;
    size_t body_start, ep_pos, ext_start, ext_end, data_byte;
} Slice;

static int parse_slice_header(const SPS *s, const PPS *p, const uint8_t *rbsp, size_t n, int nal_type,
                              Slice *sh)
{
    BR r = {rbsp, n * 8, 0, 0};
    uint8_t nd[65], nu[65];
    int i, total = 0, tmvp = 0, num_ep;

    memset(sh, 0, sizeof(*sh));
    sh->nal_type = nal_type;
    sh->first = (int)rd(&r, 1);
    if (nal_type >= 16 && nal_type <= 23)
        sh->no_output = (int)rd(&r, 1);
    sh->pps_id = (int)ue(&r);
    if (!sh->first) {
        if (p->dep_slices && rd(&r, 1))
            return -E_HEADER; /* dependent slice segment */
        sh->addr = (int)rd(&r, ceil_log2((uint32_t)(s->ctb_w * s->ctb_h)));
        if (sh->addr >= s->ctb_w * s->ctb_h)
            return -E_HEADER;
    }
    sh->body_start = r.pos;
    rd(&r, p->extra_bits);
    sh->slice_type = (int)ue(&r);
    if (sh->slice_type > 2)
        return -E_HEADER;
    if (p->output_flag)
        rd(&r, 1);
    if (nal_type != 19 && nal_type != 20) {
        rd(&r, s->log2_max_poc_lsb);
        if (!rd(&r, 1)) {
            memcpy(nd, s->st_ndelta, sizeof(nd));
            memcpy(nu, s->st_nused, sizeof(nu));
            if (st_rps(&r, s->num_st_rps, s->num_st_rps, nd, nu))
                return -E_HEADER;
            total += nu[s->num_st_rps];
        } else {
            int idx = 0;
            if (s->num_st_rps == 0)
                return -E_HEADER;
            if (s->num_st_rps > 1)
                idx = (int)rd(&r, ceil_log2((uint32_t)s->num_st_rps));
            if (idx >= s->num_st_rps)
                return -E_HEADER;
            total += s->st_nused[idx];
        }
        if (s->lt_present) {
            uint32_t nlt_sps = 0, nlt;
            if (s->num_lt_sps > 0)
                nlt_sps = ue(&r);
            nlt = ue(&r);
            if (nlt_sps > (uint32_t)s->num_lt_sps || nlt > 32)
                return -E_HEADER;
            for (i = 0; i < (int)(nlt_sps + nlt); i++) {
                int used;
                if (i < (int)nlt_sps) {
                    int idx = 0;
                    if (s->num_lt_sps > 1)
                        idx = (int)rd(&r, ceil_log2((uint32_t)s->num_lt_sps));
                    if (idx >= s->num_lt_sps)
                        return -E_HEADER;
                    used = s->lt_used[idx];
                } else {
                    rd(&r, s->log2_max_poc_lsb);
                    used = (int)rd(&r, 1);
                }
                if (rd(&r, 1))
                    ue(&r);
                total += used;
            }
        }
        if (s->tmvp)
            tmvp = (int)rd(&r, 1);
    }
    if (s->sao) {
        sh->sao_luma = (int)rd(&r, 1);
        sh->sao_chroma = (int)rd(&r, 1);
    }
    sh->max_merge = 5;
    if (sh->slice_type != SLICE_I) {
        int b = sh->slice_type == SLICE_B;
        sh->nref[0] = p->nref_def[0];
        sh->nref[1] = b ? p->nref_def[1] : 0;
        if (rd(&r, 1)) {
            sh->nref[0] = (int)ue(&r) + 1;
            if (b)
                sh->nref[1] = (int)ue(&r) + 1;
        }
        if (sh->nref[0] > 15 || sh->nref[1] > 15)
            return -E_HEADER;
        if (p->lists_mod && total > 1) {
            int bits = ceil_log2((uint32_t)total);
            if (rd(&r, 1))
                for (i = 0; i < sh->nref[0]; i++)
                    rd(&r, bits);
            if (b && rd(&r, 1))
                for (i = 0; i < sh->nref[1]; i++)
                    rd(&r, bits);
        }
        if (b)
            sh->mvd_l1_zero = (int)rd(&r, 1);
        if (p->cabac_init_present)
            sh->cabac_init = (int)rd(&r, 1);
        if (tmvp) {
            int col_l0 = 1;
            if (b)
                col_l0 = (int)rd(&r, 1);
            if ((col_l0 && sh->nref[0] > 1) || (!col_l0 && sh->nref[1] > 1))
                ue(&r);
        }
        if ((p->weighted_pred && !b) || (p->weighted_bipred && b)) {
            int l, lw[16], cw[16];
            ue(&r);
            se(&r);
            for (l = 0; l < (b ? 2 : 1); l++) {
                for (i = 0; i < sh->nref[l]; i++)
                    lw[i] = (int)rd(&r, 1);
                for (i = 0; i < sh->nref[l]; i++)
                    cw[i] = (int)rd(&r, 1);
                for (i = 0; i < sh->nref[l]; i++) {
                    if (lw[i]) {
                        se(&r);
                        se(&r);
                    }
                    if (cw[i]) {
                        se(&r); se(&r); se(&r); se(&r);
                    }
                }
            }
        }
        sh->max_merge = 5 - (int)ue(&r);
        if (sh->max_merge < 1 || sh->max_merge > 5)
            return -E_HEADER;
    }
    sh->qp = p->init_qp + se(&r);
    if (p->slice_chroma_qp_offsets) {
        se(&r);
        se(&r);
    }
    sh->deblock_disabled = p->deblock_disabled;
    if (p->deblock_override && rd(&r, 1)) {
        sh->deblock_disabled = (int)rd(&r, 1);
        if (!sh->deblock_disabled) {
            se(&r);
            se(&r);
        }
    }
    sh->lf_across = p->lf_slices;
    if (p->lf_slices && (sh->sao_luma || sh->sao_chroma || !sh->deblock_disabled))
        sh->lf_across = (int)rd(&r, 1);
    sh->ep_pos = r.pos;
    if (p->tiles || p->wpp) {
        num_ep = (int)ue(&r);
        if (num_ep > 4096)
            return -E_HEADER;
        if (num_ep) {
            int len = (int)ue(&r) + 1;
            if (len > 32)
                return -E_HEADER;
            for (i = 0; i < num_ep && !r.err; i++)
                rd(&r, len);
        }
    }
    sh->ext_start = r.pos;
    if (p->hdr_ext) {
        uint32_t len = ue(&r);
        if (len > 256)
            return -E_HEADER;
        for (i = 0; i < (int)len; i++)
            rd(&r, 8);
    }
    sh->ext_end = r.pos;
    if (rd(&r, 1) != 1)
        return -E_HEADER;
    while (r.pos & 7)
        if (rd(&r, 1))
            return -E_HEADER;
    if (r.err)
        return -E_HEADER;
    sh->data_byte = r.pos >> 3;
    return 0;
}

/* ------------------------------------------------------------------------------------------------ */
/* CABAC (9.3)                                                                                      */

static const uint8_t lps_table[64][4] = {
    {128, 176, 208, 240}, {128, 167, 197, 227}, {128, 158, 187, 216}, {123, 150, 178, 205},
    {116, 142, 169, 195}, {111, 135, 160, 185}, {105, 128, 152, 175}, {100, 122, 144, 166},
    {95, 116, 137, 158},  {90, 110, 130, 150},  {85, 104, 123, 142},  {81, 99, 117, 135},
    {77, 94, 111, 128},   {73, 89, 105, 122},   {69, 85, 100, 116},   {66, 80, 95, 110},
    {62, 76, 90, 104},    {59, 72, 86, 99},     {56, 69, 81, 94},     {53, 65, 77, 89},
    {51, 62, 73, 85},     {48, 59, 69, 80},     {46, 56, 66, 76},     {43, 53, 63, 72},
    {41, 50, 59, 69},     {39, 48, 56, 65},     {37, 45, 54, 62},     {35, 43, 51, 59},
    {33, 41, 48, 56},     {32, 39, 46, 53},     {30, 37, 43, 50},     {29, 35, 41, 48},
    {27, 33, 39, 45},     {26, 31, 37, 43},     {24, 30, 35, 41},     {23, 28, 33, 39},
    {22, 27, 32, 37},     {21, 26, 30, 35},     {20, 24, 29, 33},     {19, 23, 27, 31},
    {18, 22, 26, 30},     {17, 21, 25, 28},     {16, 20, 23, 27},     {15, 19, 22, 25},
    {14, 18, 21, 24},     {14, 17, 20, 23},     {13, 16, 19, 22},     {12, 15, 18, 21},
    {12, 14, 17, 20},     {11, 14, 16, 19},     {11, 13, 15, 18},     {10, 12, 15, 17},
    {10, 12, 14, 16},     {9, 11, 13, 15},      {9, 11, 12, 14},      {8, 10, 12, 14},
    {8, 9, 11, 13},       {7, 9, 11, 12},       {7, 9, 10, 12},       {7, 8, 10, 11},
    {6, 8, 9, 11},        {6, 7, 9, 10},        {6, 7, 8, 9},         {2, 2, 2, 2},
};

static const uint8_t trans_lps[64] = {
    0,  0,  1,  2,  2,  4,  4,  5,  6,  7,  8,  9,  9,  11, 11, 12, 13, 13, 15, 15, 16, 16,
    18, 18, 19, 19, 21, 21, 22, 22, 23, 24, 24, 25, 26, 26, 27, 27, 28, 29, 29, 30, 30, 30,
    31, 32, 32, 33, 33, 33, 34, 34, 35, 35, 35, 36, 36, 36, 37, 37, 37, 38, 38, 63,
};

typedef struct {
    const uint8_t *b;
    size_t nbytes;
    size_t next;    /* next byte to load into the cache */
    uint64_t cache; /* unread bits, left-aligned */
    int cbits;      /* how many */
    uint32_t range, offset;
    uint32_t last_range, last_offset; /* before the last terminate bin */
} Cabac;

/* position of the next unread bit; past nbytes * 8 means the decoder read beyond the data */
static inline uint64_t cpos(const Cabac *c)
{
    return (uint64_t)c->next * 8 - (uint64_t)c->cbits;
}

static inline int coverrun(const Cabac *c)
{
    return cpos(c) > (uint64_t)c->nbytes * 8;
}

static void crefill(Cabac *c)
{
    while (c->cbits <= 56) {
        uint64_t byte = c->next < c->nbytes ? c->b[c->next] : 0; /* zeros past the end, see coverrun */
        c->next++;
        c->cache |= byte << (56 - c->cbits);
        c->cbits += 8;
    }
}

static inline uint32_t cread(Cabac *c, int k) /* 1..16 bits */
{
    uint32_t v;
    if (c->cbits < k)
        crefill(c);
    v = (uint32_t)(c->cache >> (64 - k));
    c->cache <<= k;
    c->cbits -= k;
    return v;
}

static int cabac_start(Cabac *c, uint64_t bitpos)
{
    c->next = (size_t)(bitpos >> 3);
    c->cache = 0;
    c->cbits = 0;
    if (bitpos & 7)
        cread(c, (int)(bitpos & 7));
    c->range = 510;
    c->offset = cread(c, 9);
    return (c->offset >= 510 || coverrun(c)) ? -1 : 0;
}

/* -DHM_TRACE and HM_TRACE=<file>: one line per bin (C state mps bin range / B bin / T bin), the format of
 * the bin trace used to check this decoder against libde265 */
#ifdef HM_TRACE
#include <stdio.h>
FILE *hm_trace;
#define TRACE(...) do { if (hm_trace) fprintf(hm_trace, __VA_ARGS__); } while (0)
#else
#define TRACE(...) do { } while (0)
#endif

static inline int dec_bin(Cabac *c, uint8_t *ctx)
{
    int s = *ctx >> 1, mps = *ctx & 1, bin;
    uint32_t r0 = c->range;
    uint32_t lps = lps_table[s][(c->range >> 6) & 3];
    c->range -= lps;
    if (c->offset >= c->range) {
        bin = !mps;
        c->offset -= c->range;
        c->range = lps;
        if (s == 0)
            mps = !mps;
        s = trans_lps[s];
    } else {
        bin = mps;
        if (s < 62)
            s++;
    }
    TRACE("C %d %d %d %x\n", *ctx >> 1, *ctx & 1, bin, r0);
    (void)r0;
    *ctx = (uint8_t)(s << 1 | mps);
    if (c->range < 256) {
        int n = __builtin_clz(c->range) - 23;
        c->range <<= n;
        c->offset = (c->offset << n) | cread(c, n);
    }
    return bin;
}

static inline int dec_bypass(Cabac *c)
{
    c->offset = (c->offset << 1) | cread(c, 1);
    if (c->offset >= c->range) {
        c->offset -= c->range;
        TRACE("B 1\n");
        return 1;
    }
    TRACE("B 0\n");
    return 0;
}

static inline uint32_t dec_bypass_bits(Cabac *c, int n)
{
    uint32_t v = 0;
    while (n-- > 0)
        v = (v << 1) | (uint32_t)dec_bypass(c);
    return v;
}

static inline int dec_term(Cabac *c)
{
    c->last_range = c->range;
    c->last_offset = c->offset;
    c->range -= 2;
    if (c->offset >= c->range) {
        TRACE("T 1\n");
        return 1;
    }
    TRACE("T 0\n");
    if (c->range < 256) {
        c->range <<= 1;
        c->offset = (c->offset << 1) | cread(c, 1);
    }
    return 0;
}

/* context variables: same order and init values as FFmpeg's hevc/cabac.c */
enum {
    C_SAO_MERGE = 0, C_SAO_TYPE = 1, C_SPLIT_CU = 2, C_TQ_BYPASS = 5, C_SKIP = 6, C_QP_DELTA = 9,
    C_PRED_MODE = 12, C_PART_MODE = 13, C_PREV_INTRA = 17, C_CHROMA_PRED = 18, C_MERGE_FLAG = 20,
    C_MERGE_IDX = 21, C_INTER_PRED = 22, C_REF_IDX = 27, C_MVD_G0 = 31, C_MVD_G1 = 33, C_MVP = 35,
    C_NO_RESIDUAL = 36, C_SPLIT_TF = 37, C_CBF_LUMA = 40, C_CBF_CBCR = 42, C_TS_FLAG = 47,
    C_LAST_X = 53, C_LAST_Y = 71, C_SIG_GROUP = 89, C_SIG = 93, C_GT1 = 137, C_GT2 = 161,
    NCTX = 167 + 12
};

#define CNU 154
static const uint8_t init_values[3][NCTX] = {
    {
        153, 200, 139, 141, 157, 154, CNU, CNU, CNU, 154, 154, 154, CNU, 184, CNU, CNU, CNU, 184, 63, 139,
        CNU, CNU, CNU, CNU, CNU, CNU, CNU, CNU, CNU, CNU, CNU, CNU, CNU, CNU, CNU, CNU, CNU, 153, 138, 138,
        111, 141, 94, 138, 182, 154, 154, 139, 139, 139, 139, 139, 139,
        110, 110, 124, 125, 140, 153, 125, 127, 140, 109, 111, 143, 127, 111, 79, 108, 123, 63,
        110, 110, 124, 125, 140, 153, 125, 127, 140, 109, 111, 143, 127, 111, 79, 108, 123, 63,
        91, 171, 134, 141,
        111, 111, 125, 110, 110, 94, 124, 108, 124, 107, 125, 141, 179, 153, 125, 107, 125, 141, 179, 153,
        125, 107, 125, 141, 179, 153, 125, 140, 139, 182, 182, 152, 136, 152, 136, 153, 136, 139, 111, 136,
        139, 111, 141, 111,
        140, 92, 137, 138, 140, 152, 138, 139, 153, 74, 149, 92, 139, 107, 122, 152, 140, 179, 166, 182,
        140, 227, 122, 197,
        138, 153, 136, 167, 152, 152,
        154, 154, 154, 154, 154, 154, 154, 154, 154, 154, 154, 154,
    },
    {
        153, 185, 107, 139, 126, 154, 197, 185, 201, 154, 154, 154, 149, 154, 139, 154, 154, 154, 152, 139,
        110, 122, 95, 79, 63, 31, 31, 153, 153, 153, 153, 140, 198, 140, 198, 168, 79, 124, 138, 94,
        153, 111, 149, 107, 167, 154, 154, 139, 139, 139, 139, 139, 139,
        125, 110, 94, 110, 95, 79, 125, 111, 110, 78, 110, 111, 111, 95, 94, 108, 123, 108,
        125, 110, 94, 110, 95, 79, 125, 111, 110, 78, 110, 111, 111, 95, 94, 108, 123, 108,
        121, 140, 61, 154,
        155, 154, 139, 153, 139, 123, 123, 63, 153, 166, 183, 140, 136, 153, 154, 166, 183, 140, 136, 153,
        154, 166, 183, 140, 136, 153, 154, 170, 153, 123, 123, 107, 121, 107, 121, 167, 151, 183, 140, 151,
        183, 140, 140, 140,
        154, 196, 196, 167, 154, 152, 167, 182, 182, 134, 149, 136, 153, 121, 136, 137, 169, 194, 166, 167,
        154, 167, 137, 182,
        107, 167, 91, 122, 107, 167,
        154, 154, 154, 154, 154, 154, 154, 154, 154, 154, 154, 154,
    },
    {
        153, 160, 107, 139, 126, 154, 197, 185, 201, 154, 154, 154, 134, 154, 139, 154, 154, 183, 152, 139,
        154, 137, 95, 79, 63, 31, 31, 153, 153, 153, 153, 169, 198, 169, 198, 168, 79, 224, 167, 122,
        153, 111, 149, 92, 167, 154, 154, 139, 139, 139, 139, 139, 139,
        125, 110, 124, 110, 95, 94, 125, 111, 111, 79, 125, 126, 111, 111, 79, 108, 123, 93,
        125, 110, 124, 110, 95, 94, 125, 111, 111, 79, 125, 126, 111, 111, 79, 108, 123, 93,
        121, 140, 61, 154,
        170, 154, 139, 153, 139, 123, 123, 63, 124, 166, 183, 140, 136, 153, 154, 166, 183, 140, 136, 153,
        154, 166, 183, 140, 136, 153, 154, 170, 153, 138, 138, 122, 121, 122, 121, 167, 151, 183, 140, 151,
        183, 140, 140, 140,
        154, 196, 167, 167, 154, 152, 167, 182, 182, 134, 149, 136, 153, 121, 136, 122, 169, 208, 166, 167,
        154, 152, 167, 182,
        107, 167, 91, 107, 107, 167,
        154, 154, 154, 154, 154, 154, 154, 154, 154, 154, 154, 154,
    },
};

static void init_contexts(uint8_t *st, int slice_type, int cabac_init, int qp)
{
    int type = slice_type == SLICE_I ? 0 : slice_type == SLICE_P ? (cabac_init ? 2 : 1) : (cabac_init ? 1 : 2);
    int i;
    if (qp < 0)
        qp = 0;
    if (qp > 51)
        qp = 51;
    for (i = 0; i < NCTX; i++) {
        int v = init_values[type][i];
        int m = (v >> 4) * 5 - 45, n = ((v & 15) << 3) - 16;
        int pre = ((m * qp) >> 4) + n;
        int mps;
        if (pre < 1)
            pre = 1;
        if (pre > 126)
            pre = 126;
        mps = pre > 63;
        st[i] = (uint8_t)((mps ? pre - 64 : 63 - pre) << 1 | mps);
    }
}

/* ------------------------------------------------------------------------------------------------ */
/* scans (6.5.3 - 6.5.5)                                                                            */

static uint8_t diag4_x[16], diag4_y[16], diag2_x[4], diag2_y[4], diag8_x[64], diag8_y[64];
static uint8_t diag4_inv[4][4], diag2_inv[2][2], diag8_inv[8][8], horiz8_inv[8][8];
static const uint8_t horiz2_x[4] = {0, 1, 0, 1}, horiz2_y[4] = {0, 0, 1, 1};
static const uint8_t horiz4_x[16] = {0, 1, 2, 3, 0, 1, 2, 3, 0, 1, 2, 3, 0, 1, 2, 3};
static const uint8_t horiz4_y[16] = {0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3};
static int scans_ready;

static void diag_scan(int size, uint8_t *sx, uint8_t *sy)
{
    int i = 0, x = 0, y = 0;
    while (i < size * size) {
        while (y >= 0) {
            if (x < size && y < size) {
                sx[i] = (uint8_t)x;
                sy[i] = (uint8_t)y;
                i++;
            }
            y--;
            x++;
        }
        y = x;
        x = 0;
    }
}

static void init_scans(void)
{
    int i, x, y;
    if (scans_ready)
        return;
    diag_scan(2, diag2_x, diag2_y);
    diag_scan(4, diag4_x, diag4_y);
    diag_scan(8, diag8_x, diag8_y);
    for (i = 0; i < 4; i++)
        diag2_inv[diag2_y[i]][diag2_x[i]] = (uint8_t)i;
    for (i = 0; i < 16; i++)
        diag4_inv[diag4_y[i]][diag4_x[i]] = (uint8_t)i;
    for (i = 0; i < 64; i++)
        diag8_inv[diag8_y[i]][diag8_x[i]] = (uint8_t)i;
    /* horizontal scan of an 8x8 block: 4x4 sub-blocks in raster order, raster inside */
    for (y = 0; y < 8; y++)
        for (x = 0; x < 8; x++)
            horiz8_inv[y][x] = (uint8_t)((((y >> 2) * 2 + (x >> 2)) << 4) + ((y & 3) << 2) + (x & 3));
    scans_ready = 1;
}

/* ------------------------------------------------------------------------------------------------ */
/* slice data (7.3.8)                                                                               */

#define MAX_SUBS 4096

typedef struct {
    size_t start, end;  /* bytes of the substream in the RBSP */
    uint64_t stop;      /* bit after the last CABAC bit (the stop bit is stop - 1) */
    uint32_t range, offset; /* coder state before end_of_slice_segment_flag of a slice's last substream */
} Sub;

typedef struct {
    const SPS *sps;
    const PPS *pps;
    const Slice *sh;
    const Tiles *t;
    Cabac c;
    uint8_t st[NCTX];
    uint8_t *depth, *skip, *ipm; /* per min CB, per min CB, per 4x4 */
    int ctb_mask, ctb_left, ctb_up, ipm_w;
    int qp_coded, intra, part, intra_split, bypass, merge_flag, ct_depth, max_trafo_depth;
    int pu_ipm[4], ipm_c;
    int err;
} Dec;

enum { PART_2Nx2N, PART_2NxN, PART_Nx2N, PART_NxN, PART_2NxnU, PART_2NxnD, PART_nLx2N, PART_nRx2N };
enum { SCAN_DIAG, SCAN_HORIZ, SCAN_VERT };

#define BIN(i) dec_bin(&d->c, &d->st[i])
#define BYPASS() dec_bypass(&d->c)

static void fill(uint8_t *grid, int stride, int x, int y, int w, int h, uint8_t v)
{
    int j;
    for (j = 0; j < h; j++)
        memset(grid + (size_t)(y + j) * (size_t)stride + x, v, (size_t)w);
}

static void sao_syntax(Dec *d, int rx, int ry, int rs)
{
    const Tiles *t = d->t;
    int W = d->sps->ctb_w, ts = t->rs2ts[rs], ml = 0, mu = 0, c, type_c = 0;
    if (rx > 0 && rs > d->sh->addr && t->tile_id[ts] == t->tile_id[t->rs2ts[rs - 1]])
        ml = BIN(C_SAO_MERGE);
    if (ry > 0 && !ml && rs - W >= d->sh->addr && t->tile_id[ts] == t->tile_id[t->rs2ts[rs - W]])
        mu = BIN(C_SAO_MERGE);
    if (ml || mu)
        return;
    for (c = 0; c < 3; c++) {
        int type, i, absv[4], cmax;
        if (!(c == 0 ? d->sh->sao_luma : d->sh->sao_chroma))
            continue;
        if (c < 2) {
            type = BIN(C_SAO_TYPE) ? (BYPASS() ? 2 : 1) : 0;
            if (c == 1)
                type_c = type;
        } else {
            type = type_c;
        }
        if (!type)
            continue;
        cmax = (1 << ((c ? d->sps->bit_depth_c : d->sps->bit_depth) < 10 ?
                      (c ? d->sps->bit_depth_c : d->sps->bit_depth) - 5 : 5)) - 1;
        for (i = 0; i < 4; i++) {
            int v = 0;
            while (v < cmax && BYPASS())
                v++;
            absv[i] = v;
        }
        if (type == 1) {
            for (i = 0; i < 4; i++)
                if (absv[i])
                    BYPASS();
            dec_bypass_bits(&d->c, 5);
        } else if (c != 2) {
            dec_bypass_bits(&d->c, 2);
        }
    }
}

static void residual(Dec *d, int log2, int scan, int c_idx)
{
    static const uint8_t ctx_idx_map[] = {
        0, 1, 4, 5, 2, 3, 4, 5, 6, 6, 8, 8, 7, 7, 8, 8,
        1, 1, 1, 0, 1, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0,
        2, 2, 2, 2, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0,
        2, 1, 0, 0, 2, 1, 0, 0, 2, 1, 0, 0, 2, 1, 0, 0,
        2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, /* prev_sig 3 */
    };
    Cabac *c = &d->c;
    int size = 1 << log2, max = (log2 << 1) - 1, off, shf, lx = 0, ly = 0, i;
    int num_coeff, last_subset, xcg_last, ycg_last, greater1_ctx = 1;
    const uint8_t *scan_x_cg, *scan_y_cg, *scan_x_off, *scan_y_off;
    uint8_t sig_group[8][8];

    memset(sig_group, 0, sizeof(sig_group));
    if (!d->bypass && d->pps->transform_skip && log2 <= 2)
        BIN(C_TS_FLAG + (c_idx ? 1 : 0));
    if (!c_idx) {
        off = 3 * (log2 - 2) + ((log2 - 1) >> 2);
        shf = (log2 + 1) >> 2;
    } else {
        off = 15;
        shf = log2 - 2;
    }
    while (lx < max && BIN(C_LAST_X + off + (lx >> shf)))
        lx++;
    while (ly < max && BIN(C_LAST_Y + off + (ly >> shf)))
        ly++;
    if (lx > 3) {
        int n = (lx >> 1) - 1;
        lx = (1 << n) * (2 + (lx & 1)) + (int)dec_bypass_bits(c, n);
    }
    if (ly > 3) {
        int n = (ly >> 1) - 1;
        ly = (1 << n) * (2 + (ly & 1)) + (int)dec_bypass_bits(c, n);
    }
    if (lx >= size || ly >= size) {
        d->err = 1;
        return;
    }
    if (scan == SCAN_VERT) {
        int tmp = lx;
        lx = ly;
        ly = tmp;
    }
    xcg_last = lx >> 2;
    ycg_last = ly >> 2;
    if (scan == SCAN_DIAG) {
        scan_x_off = diag4_x;
        scan_y_off = diag4_y;
        num_coeff = diag4_inv[ly & 3][lx & 3];
        if (size == 4) {
            static const uint8_t zero[1] = {0};
            scan_x_cg = scan_y_cg = zero;
        } else if (size == 8) {
            num_coeff += diag2_inv[ycg_last][xcg_last] << 4;
            scan_x_cg = diag2_x;
            scan_y_cg = diag2_y;
        } else if (size == 16) {
            num_coeff += diag4_inv[ycg_last][xcg_last] << 4;
            scan_x_cg = diag4_x;
            scan_y_cg = diag4_y;
        } else {
            num_coeff += diag8_inv[ycg_last][xcg_last] << 4;
            scan_x_cg = diag8_x;
            scan_y_cg = diag8_y;
        }
    } else if (scan == SCAN_HORIZ) {
        scan_x_cg = horiz2_x;
        scan_y_cg = horiz2_y;
        scan_x_off = horiz4_x;
        scan_y_off = horiz4_y;
        num_coeff = horiz8_inv[ly][lx];
    } else {
        scan_x_cg = horiz2_y;
        scan_y_cg = horiz2_x;
        scan_x_off = horiz4_y;
        scan_y_off = horiz4_x;
        num_coeff = horiz8_inv[lx][ly];
    }
    num_coeff++;
    last_subset = (num_coeff - 1) >> 4;

    for (i = last_subset; i >= 0; i--) {
        int n, m, x_cg = scan_x_cg[i], y_cg = scan_y_cg[i], implicit_nz = 0, prev_sig = 0, n_end;
        int offset = i << 4, last_scan_pos;
        uint8_t sig_idx[16];
        int nb_sig = 0;

        if (i < last_subset && i > 0) {
            int ctx_cg = 0;
            if (x_cg < (1 << (log2 - 2)) - 1)
                ctx_cg += sig_group[x_cg + 1][y_cg];
            if (y_cg < (1 << (log2 - 2)) - 1)
                ctx_cg += sig_group[x_cg][y_cg + 1];
            sig_group[x_cg][y_cg] = (uint8_t)BIN(C_SIG_GROUP + (ctx_cg ? 1 : 0) + (c_idx ? 2 : 0));
            implicit_nz = 1;
        } else {
            sig_group[x_cg][y_cg] = (uint8_t)((x_cg == xcg_last && y_cg == ycg_last) || (x_cg == 0 && y_cg == 0));
        }
        last_scan_pos = num_coeff - offset - 1;
        if (i == last_subset) {
            n_end = last_scan_pos - 1;
            sig_idx[0] = (uint8_t)last_scan_pos;
            nb_sig = 1;
        } else {
            n_end = 15;
        }
        if (x_cg < ((1 << log2) - 1) >> 2)
            prev_sig = !!sig_group[x_cg + 1][y_cg];
        if (y_cg < ((1 << log2) - 1) >> 2)
            prev_sig += (!!sig_group[x_cg][y_cg + 1]) << 1;

        if (sig_group[x_cg][y_cg] && n_end >= 0) {
            const uint8_t *map;
            int scf = c_idx ? 27 : 0;
            if (log2 == 2) {
                map = ctx_idx_map;
            } else {
                map = &ctx_idx_map[(prev_sig + 1) << 4];
                if (c_idx == 0) {
                    if (x_cg > 0 || y_cg > 0)
                        scf += 3;
                    scf += log2 == 3 ? (scan == SCAN_DIAG ? 9 : 15) : 21;
                } else {
                    scf += log2 == 3 ? 9 : 12;
                }
            }
            for (n = n_end; n > 0; n--) {
                int xc = scan_x_off[n], yc = scan_y_off[n];
                if (BIN(C_SIG + map[(yc << 2) + xc] + scf)) {
                    sig_idx[nb_sig++] = (uint8_t)n;
                    implicit_nz = 0;
                }
            }
            if (!implicit_nz) {
                if (i == 0)
                    scf = c_idx ? 27 : 0;
                else
                    scf = 2 + scf;
                if (BIN(C_SIG + scf))
                    sig_idx[nb_sig++] = 0;
            } else {
                sig_idx[nb_sig++] = 0;
            }
        }

        if (nb_sig) {
            int ctx_set = (i > 0 && c_idx == 0) ? 2 : 0, first_g1 = -1, rice = 0, sign_hidden;
            uint8_t g1[8];
            if (i != last_subset && greater1_ctx == 0)
                ctx_set++;
            greater1_ctx = 1;
            for (m = 0; m < (nb_sig > 8 ? 8 : nb_sig); m++) {
                g1[m] = (uint8_t)BIN(C_GT1 + (ctx_set << 2) + greater1_ctx + (c_idx ? 16 : 0));
                if (g1[m]) {
                    greater1_ctx = 0;
                    if (first_g1 < 0)
                        first_g1 = m;
                } else if (greater1_ctx > 0 && greater1_ctx < 3) {
                    greater1_ctx++;
                }
            }
            sign_hidden = !d->bypass && (sig_idx[0] - sig_idx[nb_sig - 1] >= 4);
            if (first_g1 >= 0)
                g1[first_g1] = (uint8_t)(g1[first_g1] + BIN(C_GT2 + ctx_set + (c_idx ? 4 : 0)));
            dec_bypass_bits(c, (d->pps->sign_hiding && sign_hidden) ? nb_sig - 1 : nb_sig);
            for (m = 0; m < nb_sig; m++) {
                int base, need;
                if (m < 8) {
                    base = 1 + g1[m];
                    need = base == ((m == first_g1) ? 3 : 2);
                } else {
                    base = 1;
                    need = 1;
                }
                if (need) {
                    int prefix = 0, rem;
                    while (prefix < 32 && BYPASS())
                        prefix++;
                    if (prefix >= 32) {
                        d->err = 1;
                        return;
                    }
                    if (prefix <= 3) {
                        rem = (prefix << rice) + (int)dec_bypass_bits(c, rice);
                    } else {
                        int k = prefix - 3 + rice;
                        if (k > 22) {
                            d->err = 1;
                            return;
                        }
                        rem = (((1 << (prefix - 3)) + 3 - 1) << rice) + (int)dec_bypass_bits(c, k);
                    }
                    if (base + rem > (3 << rice) && rice < 4)
                        rice++;
                }
            }
        }
    }
}

static void transform_unit(Dec *d, int log2, int blk, int cbf_luma, int cb, int cr, int tu_ipm)
{
    if (!(cbf_luma || cb || cr))
        return;
    if (d->pps->cu_qp_delta && !d->qp_coded) {
        int v = 0;
        while (v < 5 && BIN(C_QP_DELTA + (v ? 1 : 0)))
            v++;
        if (v == 5) {
            int k = 0;
            while (k < 32 && BYPASS())
                k++;
            if (k >= 32) {
                d->err = 1;
                return;
            }
            v += (1 << k) - 1 + (int)dec_bypass_bits(&d->c, k);
        }
        if (v)
            BYPASS();
        d->qp_coded = 1;
    }
    {
        int scan = SCAN_DIAG, scan_c = SCAN_DIAG;
        if (d->intra && log2 < 4) {
            if (tu_ipm >= 6 && tu_ipm <= 14)
                scan = SCAN_VERT;
            else if (tu_ipm >= 22 && tu_ipm <= 30)
                scan = SCAN_HORIZ;
            if (d->ipm_c >= 6 && d->ipm_c <= 14)
                scan_c = SCAN_VERT;
            else if (d->ipm_c >= 22 && d->ipm_c <= 30)
                scan_c = SCAN_HORIZ;
        }
        if (cbf_luma)
            residual(d, log2, scan, 0);
        if (log2 > 2) {
            if (cb)
                residual(d, log2 - 1, scan_c, 1);
            if (cr)
                residual(d, log2 - 1, scan_c, 2);
        } else if (blk == 3) {
            if (cb)
                residual(d, 2, scan_c, 1);
            if (cr)
                residual(d, 2, scan_c, 2);
        }
    }
}

static void transform_tree(Dec *d, int log2, int depth, int blk, int pcb, int pcr, int tu_ipm)
{
    const SPS *s = d->sps;
    int split, cb = pcb, cr = pcr;
    if (d->err)
        return;
    if (d->intra_split && depth == 1)
        tu_ipm = d->pu_ipm[blk];
    if (log2 <= s->log2_max_tb && log2 > s->log2_min_tb && depth < d->max_trafo_depth &&
        !(d->intra_split && depth == 0)) {
        split = BIN(C_SPLIT_TF + 5 - log2);
    } else {
        int inter_split = s->max_th_inter == 0 && !d->intra && d->part != PART_2Nx2N && depth == 0;
        split = log2 > s->log2_max_tb || (d->intra_split && depth == 0) || inter_split;
    }
    if (log2 > 2) {
        cb = (depth == 0 || pcb) ? BIN(C_CBF_CBCR + depth) : 0;
        cr = (depth == 0 || pcr) ? BIN(C_CBF_CBCR + depth) : 0;
    }
    if (split) {
        int k;
        if (log2 <= 2) {
            d->err = 1;
            return;
        }
        for (k = 0; k < 4; k++)
            transform_tree(d, log2 - 1, depth + 1, k, cb, cr, tu_ipm);
    } else {
        int cbf_luma = 1;
        if (d->intra || depth != 0 || cb || cr)
            cbf_luma = BIN(C_CBF_LUMA + (depth == 0 ? 1 : 0));
        transform_unit(d, log2, blk, cbf_luma, cb, cr, tu_ipm);
    }
}

static void mvd_coding(Dec *d)
{
    int gx = BIN(C_MVD_G0), gy = BIN(C_MVD_G0), g1x = 0, g1y = 0, comp;
    if (gx)
        g1x = BIN(C_MVD_G1 + 1);
    if (gy)
        g1y = BIN(C_MVD_G1 + 1);
    for (comp = 0; comp < 2; comp++) {
        int g0 = comp ? gy : gx, g1 = comp ? g1y : g1x;
        if (g1) {
            int k = 1;
            while (BYPASS()) {
                if (++k > 31) {
                    d->err = 1;
                    return;
                }
            }
            dec_bypass_bits(&d->c, k);
        }
        if (g0)
            BYPASS();
    }
}

static void prediction_unit(Dec *d, int w, int h, int skip)
{
    const Slice *sh = d->sh;
    int idc = 0; /* 0 L0, 1 L1, 2 BI */
    if (!skip)
        d->merge_flag = BIN(C_MERGE_FLAG);
    if (skip || d->merge_flag) {
        if (sh->max_merge > 1 && BIN(C_MERGE_IDX)) {
            int i = 1;
            while (i < sh->max_merge - 1 && BYPASS())
                i++;
        }
        return;
    }
    if (sh->slice_type == SLICE_B) {
        if (w + h == 12)
            idc = BIN(C_INTER_PRED + 4);
        else if (BIN(C_INTER_PRED + d->ct_depth))
            idc = 2;
        else
            idc = BIN(C_INTER_PRED + 4);
    }
    {
        int l;
        for (l = 0; l < 2; l++) {
            if ((l == 0 && idc == 1) || (l == 1 && idc == 0))
                continue;
            if (sh->nref[l] > 1) {
                int i = 0, max = sh->nref[l] - 1, max_ctx = max < 2 ? max : 2;
                while (i < max_ctx && BIN(C_REF_IDX + i))
                    i++;
                if (i == 2)
                    while (i < max && BYPASS())
                        i++;
            }
            if (!(l == 1 && sh->mvd_l1_zero && idc == 2))
                mvd_coding(d);
            BIN(C_MVP);
        }
    }
}

static int intra_mode(Dec *d, int x0, int y0, int pb, int prev_flag, int mpm, int rem)
{
    int xp = x0 >> 2, yp = y0 >> 2, n = pb >> 2, left = 1, up = 1, cand[3], mode, i;
    if ((x0 & d->ctb_mask) || d->ctb_left)
        left = d->ipm[(size_t)yp * (size_t)d->ipm_w + xp - 1];
    if (y0 & d->ctb_mask)
        up = d->ipm[(size_t)(yp - 1) * (size_t)d->ipm_w + xp];
    if (left == up) {
        if (left < 2) {
            cand[0] = 0;
            cand[1] = 1;
            cand[2] = 26;
        } else {
            cand[0] = left;
            cand[1] = 2 + ((left - 2 - 1 + 32) & 31);
            cand[2] = 2 + ((left - 2 + 1) & 31);
        }
    } else {
        cand[0] = left;
        cand[1] = up;
        if (left != 0 && up != 0)
            cand[2] = 0;
        else if (left != 1 && up != 1)
            cand[2] = 1;
        else
            cand[2] = 26;
    }
    if (prev_flag) {
        mode = cand[mpm];
    } else {
        int t;
        if (cand[0] > cand[1]) { t = cand[0]; cand[0] = cand[1]; cand[1] = t; }
        if (cand[0] > cand[2]) { t = cand[0]; cand[0] = cand[2]; cand[2] = t; }
        if (cand[1] > cand[2]) { t = cand[1]; cand[1] = cand[2]; cand[2] = t; }
        mode = rem;
        for (i = 0; i < 3; i++)
            if (mode >= cand[i])
                mode++;
    }
    if (n < 1)
        n = 1;
    fill(d->ipm, d->ipm_w, xp, yp, n, n, (uint8_t)mode);
    return mode;
}

static int part_mode(Dec *d, int log2)
{
    const SPS *s = d->sps;
    if (BIN(C_PART_MODE))
        return PART_2Nx2N;
    if (log2 == s->log2_min_cb) {
        if (d->intra)
            return PART_NxN;
        if (BIN(C_PART_MODE + 1))
            return PART_2NxN;
        if (log2 == 3)
            return PART_Nx2N;
        if (BIN(C_PART_MODE + 2))
            return PART_Nx2N;
        return PART_NxN;
    }
    if (!s->amp) {
        if (BIN(C_PART_MODE + 1))
            return PART_2NxN;
        return PART_Nx2N;
    }
    if (BIN(C_PART_MODE + 1)) {
        if (BIN(C_PART_MODE + 3))
            return PART_2NxN;
        return BYPASS() ? PART_2NxnD : PART_2NxnU;
    }
    if (BIN(C_PART_MODE + 3))
        return PART_Nx2N;
    return BYPASS() ? PART_nRx2N : PART_nLx2N;
}

static void coding_unit(Dec *d, int x0, int y0, int log2, int depth)
{
    const SPS *s = d->sps;
    int size = 1 << log2, xcb = x0 >> s->log2_min_cb, ycb = y0 >> s->log2_min_cb;
    int len = size >> s->log2_min_cb, W = s->min_cb_w, skip = 0;

    d->ct_depth = depth;
    d->bypass = d->pps->transquant_bypass ? BIN(C_TQ_BYPASS) : 0;
    if (d->sh->slice_type != SLICE_I) {
        int inc = 0;
        if ((x0 & d->ctb_mask) || d->ctb_left)
            inc += d->skip[(size_t)ycb * (size_t)W + xcb - 1];
        if ((y0 & d->ctb_mask) || d->ctb_up)
            inc += d->skip[(size_t)(ycb - 1) * (size_t)W + xcb];
        skip = BIN(C_SKIP + inc);
    }
    fill(d->skip, W, xcb, ycb, len, len, (uint8_t)skip);
    d->intra = 0;
    d->part = PART_2Nx2N;
    d->intra_split = 0;
    d->merge_flag = 0;
    if (skip) {
        prediction_unit(d, size, size, 1);
        fill(d->ipm, d->ipm_w, x0 >> 2, y0 >> 2, size >> 2, size >> 2, 1);
    } else {
        int pcm = 0;
        d->intra = d->sh->slice_type == SLICE_I ? 1 : BIN(C_PRED_MODE);
        if (!d->intra || log2 == s->log2_min_cb)
            d->part = part_mode(d, log2);
        d->intra_split = d->intra && d->part == PART_NxN;
        if (d->intra) {
            if (d->part == PART_2Nx2N && s->pcm && log2 >= s->pcm_log2_min && log2 <= s->pcm_log2_max)
                pcm = dec_term(&d->c);
            if (pcm) {
                /* pcm_alignment_zero_bits up to the byte, the samples, then a new arithmetic decoder */
                uint64_t bits = (uint64_t)size * size * s->pcm_bd + (uint64_t)(size * size / 2) * s->pcm_bdc;
                uint64_t start = (cpos(&d->c) + 7) & ~(uint64_t)7;
                fill(d->ipm, d->ipm_w, x0 >> 2, y0 >> 2, size >> 2, size >> 2, 1);
                if (cabac_start(&d->c, start + bits))
                    d->err = 1;
            } else {
                int nparts = d->intra_split ? 4 : 1, pb = size >> d->intra_split, i, flags[4];
                static const uint8_t chroma_tab[4] = {0, 26, 10, 1};
                int cm;
                for (i = 0; i < nparts; i++)
                    flags[i] = BIN(C_PREV_INTRA);
                for (i = 0; i < nparts; i++) {
                    int mpm = 0, rem = 0;
                    if (flags[i]) {
                        while (mpm < 2 && BYPASS())
                            mpm++;
                    } else {
                        rem = (int)dec_bypass_bits(&d->c, 5);
                    }
                    d->pu_ipm[i] = intra_mode(d, x0 + pb * (i & 1), y0 + pb * (i >> 1), pb, flags[i], mpm, rem);
                }
                cm = BIN(C_CHROMA_PRED) ? (int)dec_bypass_bits(&d->c, 2) : 4;
                if (cm == 4)
                    d->ipm_c = d->pu_ipm[0];
                else
                    d->ipm_c = d->pu_ipm[0] == chroma_tab[cm] ? 34 : chroma_tab[cm];
            }
        } else {
            int h = size >> 1, q = size >> 2;
            fill(d->ipm, d->ipm_w, x0 >> 2, y0 >> 2, size >> 2, size >> 2, 1);
            switch (d->part) {
            case PART_2Nx2N: prediction_unit(d, size, size, 0); break;
            case PART_2NxN: prediction_unit(d, size, h, 0); prediction_unit(d, size, h, 0); break;
            case PART_Nx2N: prediction_unit(d, h, size, 0); prediction_unit(d, h, size, 0); break;
            case PART_2NxnU: prediction_unit(d, size, q, 0); prediction_unit(d, size, size - q, 0); break;
            case PART_2NxnD: prediction_unit(d, size, size - q, 0); prediction_unit(d, size, q, 0); break;
            case PART_nLx2N: prediction_unit(d, q, size, 0); prediction_unit(d, size - q, size, 0); break;
            case PART_nRx2N: prediction_unit(d, size - q, size, 0); prediction_unit(d, q, size, 0); break;
            default:
                prediction_unit(d, h, h, 0); prediction_unit(d, h, h, 0);
                prediction_unit(d, h, h, 0); prediction_unit(d, h, h, 0);
                break;
            }
        }
        if (!pcm) {
            int rqt = 1;
            if (!d->intra && !(d->part == PART_2Nx2N && d->merge_flag))
                rqt = BIN(C_NO_RESIDUAL);
            if (rqt) {
                d->max_trafo_depth = d->intra ? s->max_th_intra + d->intra_split : s->max_th_inter;
                transform_tree(d, log2, 0, 0, 0, 0, d->pu_ipm[0]);
            }
        }
    }
    fill(d->depth, W, xcb, ycb, len, len, (uint8_t)depth);
}

static void coding_quadtree(Dec *d, int x0, int y0, int log2, int depth)
{
    const SPS *s = d->sps;
    int size = 1 << log2, split;
    if (d->err)
        return;
    if (x0 + size <= s->width && y0 + size <= s->height && log2 > s->log2_min_cb) {
        int inc = 0, xcb = x0 >> s->log2_min_cb, ycb = y0 >> s->log2_min_cb, W = s->min_cb_w;
        if ((x0 & d->ctb_mask) || d->ctb_left)
            inc += d->depth[(size_t)ycb * (size_t)W + xcb - 1] > depth;
        if ((y0 & d->ctb_mask) || d->ctb_up)
            inc += d->depth[(size_t)(ycb - 1) * (size_t)W + xcb] > depth;
        split = BIN(C_SPLIT_CU + inc);
    } else {
        split = log2 > s->log2_min_cb;
    }
    if (d->pps->cu_qp_delta && log2 >= s->log2_ctb - d->pps->diff_qp_depth)
        d->qp_coded = 0;
    if (split) {
        int h = size >> 1;
        coding_quadtree(d, x0, y0, log2 - 1, depth + 1);
        if (x0 + h < s->width)
            coding_quadtree(d, x0 + h, y0, log2 - 1, depth + 1);
        if (y0 + h < s->height)
            coding_quadtree(d, x0, y0 + h, log2 - 1, depth + 1);
        if (x0 + h < s->width && y0 + h < s->height)
            coding_quadtree(d, x0 + h, y0 + h, log2 - 1, depth + 1);
    } else {
        coding_unit(d, x0, y0, log2, depth);
    }
}

static int bit_at(const uint8_t *b, uint64_t pos)
{
    return (b[pos >> 3] >> (7 - (pos & 7))) & 1;
}

/* After a terminate bin of 1 at bit position stop: the stop/alignment bit is stop - 1, zeros up to the
 * byte. Returns the byte where the next data starts, or 0 on a bad pattern. */
static size_t check_stop(const uint8_t *b, size_t nbytes, uint64_t stop)
{
    uint64_t p;
    if (stop == 0 || stop > (uint64_t)nbytes * 8 || !bit_at(b, stop - 1))
        return 0;
    for (p = stop; p & 7; p++)
        if (bit_at(b, p))
            return 0;
    return (size_t)(p >> 3);
}

/* Decode the slice data. Fills subs[] with the substreams (one per tile). Returns their number. */
static int decode_slice(Dec *d, const uint8_t *rbsp, size_t n, Sub *subs, int max_subs, int *ts_end)
{
    const SPS *s = d->sps;
    const Tiles *t = d->t;
    int W = s->ctb_w, total = W * s->ctb_h, ts = t->rs2ts[d->sh->addr], ts0 = ts, nsub = 0;
    size_t start = d->sh->data_byte;

    d->c.b = rbsp;
    d->c.nbytes = n;
    d->ctb_mask = (1 << s->log2_ctb) - 1;
    d->err = 0;
    if (ts0 > 0 && t->tile_id[ts0] == t->tile_id[ts0 - 1])
        return -E_LAYOUT; /* slice starts inside a tile */
    init_contexts(d->st, d->sh->slice_type, d->sh->cabac_init, d->sh->qp);
    if (cabac_start(&d->c, (uint64_t)start * 8))
        return -E_CABAC;
    for (;;) {
        int rs = t->ts2rs[ts], rx = rs % W, ry = rs / W, end;
        d->ctb_left = rx > 0 && t->tile_id[ts] == t->tile_id[t->rs2ts[rs - 1]] && t->rs2ts[rs - 1] >= ts0;
        d->ctb_up = ry > 0 && t->tile_id[ts] == t->tile_id[t->rs2ts[rs - W]] && t->rs2ts[rs - W] >= ts0;
        if (d->sh->sao_luma || d->sh->sao_chroma)
            sao_syntax(d, rx, ry, rs);
        coding_quadtree(d, rx << s->log2_ctb, ry << s->log2_ctb, s->log2_ctb, 0);
        if (d->err || coverrun(&d->c))
            return -E_CABAC;
        end = dec_term(&d->c);
        ts++;
        if (end || ts >= total || t->tile_id[ts] != t->tile_id[ts - 1]) {
            size_t next;
            uint32_t range = d->c.last_range, offset = d->c.last_offset;
            if (!end) {
                if (ts >= total || !dec_term(&d->c)) /* end_of_subset_one_bit */
                    return -E_CABAC;
            }
            next = check_stop(rbsp, n, cpos(&d->c));
            if (!next || nsub >= max_subs)
                return -E_CABAC;
            subs[nsub].start = start;
            subs[nsub].end = next;
            subs[nsub].stop = cpos(&d->c);
            subs[nsub].range = range;
            subs[nsub].offset = offset;
            nsub++;
            if (end) {
                size_t k;
                if (ts < total && t->tile_id[ts] == t->tile_id[ts - 1])
                    return -E_LAYOUT; /* slice ends inside a tile */
                for (k = next; k < n; k++) /* only cabac_zero_words may follow */
                    if (rbsp[k])
                        return -E_CABAC;
                break;
            }
            start = next;
            init_contexts(d->st, d->sh->slice_type, d->sh->cabac_init, d->sh->qp);
            if (cabac_start(&d->c, (uint64_t)start * 8))
                return -E_CABAC;
        }
    }
    *ts_end = ts;
    return nsub;
}

/* ------------------------------------------------------------------------------------------------ */
/* rewriting                                                                                        */

/* add delta * (weight of bit pos) to the big-endian bit string b; 0 if it under- or overflows */
static int add_at_bit(uint8_t *b, uint64_t pos, long delta)
{
    long carry = delta * (1L << (7 - (pos & 7)));
    long j = (long)(pos >> 3);
    for (; j >= 0 && carry; j--) {
        long v = (long)b[j] + carry;
        long low = v & 0xff;
        b[j] = (uint8_t)low;
        carry = (v - low) / 256;
    }
    return carry == 0;
}

/* Rewrite the end of a tile that ended with end_of_slice_segment_flag = 1 so that it ends with
 * end_of_slice_segment_flag = 0, end_of_subset_one_bit = 1. buf holds the substream from its first byte,
 * with one spare zero byte; stop is relative to buf. Returns the new length in bytes, 0 on failure.
 *
 * At the old flag the decoder had range R and offset O >= R - 2 (bin 1) and had read up to stop. The new
 * bins: 0 needs O' < R - 2; then range R - 2, renormalised if below 256, and 1 needs the offset to reach
 * the new range - 2.
 * - R >= 258: no renormalisation between them, O' in [R - 4, R - 2). O' = O - 2 keeps the parity, so the
 *   last bit read stays the stop bit.
 * - R in {256, 257}: one bit is read between them, so O' = R - 3 and that bit = 1 (the new stop bit):
 *   2O' + 1 = 2(R - 3) + 1.
 * Changing the code value by less than the final interval width keeps all earlier bins. */
static size_t rewrite_tail(uint8_t *buf, size_t len, uint64_t stop, uint32_t R, uint32_t O)
{
    uint64_t new_stop;
    if (O + 2 < R || O >= R)
        return 0;
    if (R >= 258) {
        if (!add_at_bit(buf, stop - 1, -2))
            return 0;
        new_stop = stop;
    } else {
        long delta = 2L * ((long)R - 3) + 1 - 2L * (long)O;
        if (stop >> 3 > len) /* the spare byte */
            return 0;
        if (!add_at_bit(buf, stop, delta))
            return 0;
        new_stop = stop + 1;
    }
    if (!bit_at(buf, new_stop - 1))
        return 0;
    {
        uint64_t p;
        for (p = new_stop; p & 7; p++)
            if (bit_at(buf, p))
                return 0;
    }
    return (size_t)((new_stop + 7) >> 3);
}

/* emulation prevention; returns bytes written or 0 if out of space */
static size_t escape(uint8_t *out, size_t cap, const uint8_t *in, size_t n)
{
    size_t i, o = 0;
    int zeros = 0;
    for (i = 0; i < n; i++) {
        if (zeros >= 2 && in[i] <= 3) {
            if (o >= cap)
                return 0;
            out[o++] = 3;
            zeros = 0;
        }
        if (o >= cap)
            return 0;
        out[o++] = in[i];
        zeros = in[i] ? 0 : zeros + 1;
    }
    return o;
}

static size_t escaped_size(const uint8_t *in, size_t n)
{
    size_t i, o = 0;
    int zeros = 0;
    for (i = 0; i < n; i++) {
        if (zeros >= 2 && in[i] <= 3) {
            o++;
            zeros = 0;
        }
        o++;
        zeros = in[i] ? 0 : zeros + 1;
    }
    return o;
}

static size_t unescape(uint8_t *out, const uint8_t *in, size_t n)
{
    size_t i, o = 0;
    int zeros = 0;
    for (i = 0; i < n; i++) {
        if (zeros >= 2 && in[i] == 3) {
            zeros = 0;
            continue;
        }
        out[o++] = in[i];
        zeros = in[i] ? 0 : zeros + 1;
    }
    return o;
}

typedef struct {
    uint8_t *b;
    size_t cap, pos; /* bits */
} BW;

static void wbits(BW *w, uint32_t v, int n)
{
    while (n-- > 0) {
        size_t byte = w->pos >> 3;
        if (byte < w->cap) {
            if (!(w->pos & 7))
                w->b[byte] = 0;
            if ((v >> n) & 1)
                w->b[byte] |= (uint8_t)(0x80 >> (w->pos & 7));
        }
        w->pos++;
    }
}

static void wue(BW *w, uint32_t v)
{
    uint64_t x = (uint64_t)v + 1;
    int n = 0;
    while ((x >> n) > 1)
        n++;
    wbits(w, 0, n);
    wbits(w, (uint32_t)x, n + 1);
}

static void copy_bits(BW *w, const uint8_t *b, size_t from, size_t to)
{
    while (from < to) {
        wbits(w, (uint32_t)bit_at(b, from), 1);
        from++;
    }
}

static int bits_equal(const uint8_t *a, size_t a0, const uint8_t *b, size_t b0, size_t n)
{
    size_t i;
    for (i = 0; i < n; i++)
        if (bit_at(a, a0 + i) != bit_at(b, b0 + i))
            return 0;
    return 1;
}

/* ------------------------------------------------------------------------------------------------ */
/* merger                                                                                           */

#define MAX_NALS 256

typedef struct {
    SPS sps[MAX_SPS];
    PPS pps[MAX_PPS];
    int gen;
    Tiles tiles;
    uint8_t *grid;
    size_t grid_size;
    Sub subs[MAX_SUBS];
    long stats[16];
} Merger;

API void *hm_new(void)
{
#ifdef HM_TRACE
    if (getenv("HM_TRACE"))
        hm_trace = fopen(getenv("HM_TRACE"), "w");
#endif
    init_scans();
    return calloc(1, sizeof(Merger));
}

API void hm_free(void *p)
{
    Merger *m = p;
    int i;
    if (!m)
        return;
    for (i = 0; i < MAX_SPS; i++)
        free(m->sps[i].raw);
    for (i = 0; i < MAX_PPS; i++)
        free(m->pps[i].raw);
    free(m->tiles.rs2ts);
    free(m->grid);
    free(m);
}

/* a parameter set NAL unit (payload after the 2-byte header, escaped) */
static void param_set(Merger *m, int type, const uint8_t *p, size_t n)
{
    uint8_t *rbsp = malloc(n + 8);
    size_t len;
    BR r;
    uint32_t id;
    if (!rbsp)
        return;
    len = unescape(rbsp, p, n);
    memset(rbsp + len, 0, 8);
    r.b = rbsp;
    r.nbits = len * 8;
    r.pos = 0;
    r.err = 0;
    if (type == 33) {
        int max_sub;
        rd(&r, 4);
        max_sub = (int)rd(&r, 3);
        rd(&r, 1);
        {
            int profile;
            uint32_t compat;
            skip_ptl(&r, max_sub, &profile, &compat);
        }
        id = ue(&r);
        if (!r.err && id < MAX_SPS) {
            SPS *s = &m->sps[id];
            if (!(s->raw && s->raw_len == n && !memcmp(s->raw, p, n))) {
                uint8_t *raw = malloc(n);
                if (raw) {
                    memcpy(raw, p, n);
                    free(s->raw);
                    parse_sps(s, rbsp, len); /* invalid on failure */
                    s->raw = raw;
                    s->raw_len = n;
                    s->gen = ++m->gen;
                }
            }
        }
    } else {
        id = ue(&r);
        if (!r.err && id < MAX_PPS) {
            PPS *q = &m->pps[id];
            if (!(q->raw && q->raw_len == n && !memcmp(q->raw, p, n))) {
                uint8_t *raw = malloc(n);
                if (raw) {
                    memcpy(raw, p, n);
                    free(q->raw);
                    parse_pps(q, rbsp, len);
                    q->raw = raw;
                    q->raw_len = n;
                    q->gen = ++m->gen;
                }
            }
        }
    }
    free(rbsp);
}

typedef struct {
    const uint8_t *p; /* NAL unit, from its header */
    size_t n;
    int type;
} Nal;

static int split_nals(const uint8_t *in, size_t n, Nal *nals, int max)
{
    size_t i = 0, start = 0;
    int count = 0, have = 0;
    while (i + 3 <= n) {
        if (in[i] == 0 && in[i + 1] == 0 && in[i + 2] == 1) {
            if (have) {
                size_t end = i;
                while (end > start && in[end - 1] == 0)
                    end--;
                if (count >= max)
                    return -1;
                nals[count].p = in + start;
                nals[count].n = end - start;
                count++;
            }
            i += 3;
            start = i;
            have = 1;
        } else {
            i++;
        }
    }
    if (have) {
        size_t end = n;
        while (end > start && in[end - 1] == 0)
            end--;
        if (count >= max)
            return -1;
        nals[count].p = in + start;
        nals[count].n = end - start;
        count++;
    }
    for (i = 0; i < (size_t)count; i++)
        nals[i].type = nals[i].n >= 2 ? (nals[i].p[0] >> 1) & 0x3f : -1;
    return count;
}

typedef struct {
    uint8_t *rbsp; /* payload after the 2-byte header, unescaped */
    size_t n;
    Slice sh;
    int first_sub, nsub, ts_end;
} SliceBuf;

static int ensure_grid(Merger *m, const SPS *s)
{
    size_t need = 2 * (size_t)s->min_cb_w * (size_t)s->min_cb_h + (size_t)(s->width >> 2) * (size_t)(s->height >> 2);
    if (need > m->grid_size) {
        uint8_t *g = realloc(m->grid, need);
        if (!g)
            return -E_NOMEM;
        m->grid = g;
        m->grid_size = need;
    }
    return 0;
}

/* The grids are shared by all slices of a picture: each slice writes and reads only its own tiles. */
static void setup_dec(Merger *m, Dec *d, const SPS *s, const PPS *p, const Slice *sh)
{
    size_t cb = (size_t)s->min_cb_w * (size_t)s->min_cb_h;
    memset(d, 0, sizeof(*d));
    d->sps = s;
    d->pps = p;
    d->sh = sh;
    d->t = &m->tiles;
    d->depth = m->grid;
    d->skip = m->grid + cb;
    d->ipm = m->grid + 2 * cb;
    d->ipm_w = s->width >> 2;
}

/* Unescape one VCL NAL unit and parse its slice header. */
static int prepare_vcl(Merger *m, const Nal *nal, SliceBuf *sb, const SPS **ps, const PPS **pp)
{
    BR r;
    uint32_t pps_id;
    const PPS *p;
    const SPS *s;
    int ret;

    sb->rbsp = malloc(nal->n + 8);
    if (!sb->rbsp)
        return -E_NOMEM;
    sb->n = unescape(sb->rbsp, nal->p + 2, nal->n - 2);
    memset(sb->rbsp + sb->n, 0, 8);
    r.b = sb->rbsp;
    r.nbits = sb->n * 8;
    r.pos = 0;
    r.err = 0;
    rd(&r, 1);
    if (nal->type >= 16 && nal->type <= 23)
        rd(&r, 1);
    pps_id = ue(&r);
    if (r.err || pps_id >= MAX_PPS || !m->pps[pps_id].valid)
        return -E_NOPARAM;
    p = &m->pps[pps_id];
    if (!m->sps[p->sps_id].valid)
        return -E_NOPARAM;
    s = &m->sps[p->sps_id];
    if (!p->tiles || p->wpp)
        return -E_PPS;
    ret = parse_slice_header(s, p, sb->rbsp, sb->n, nal->type, &sb->sh);
    if (ret)
        return ret;
    ret = build_tiles(&m->tiles, s, p);
    if (ret)
        return ret;
    *ps = s;
    *pp = p;
    return 0;
}

typedef struct {
    Merger *m;
    const SPS *s;
    const PPS *p;
    SliceBuf *sb;
    Sub *subs;
    int max_subs, ret;
} Job;

static void *decode_job(void *arg)
{
    Job *j = arg;
    Dec d;
    setup_dec(j->m, &d, j->s, j->p, &j->sb->sh);
    j->ret = decode_slice(&d, j->sb->rbsp, j->sb->n, j->subs, j->max_subs, &j->sb->ts_end);
    if (j->ret >= 0) {
        j->sb->nsub = j->ret;
        j->ret = 0;
    }
    return NULL;
}

/* Parse one VCL NAL unit's header and data (used to check a merged slice). */
static int parse_vcl(Merger *m, const Nal *nal, SliceBuf *sb, Sub *subs, int max_subs)
{
    Job j;
    int ret = prepare_vcl(m, nal, sb, &j.s, &j.p);
    if (ret)
        return ret;
    ret = ensure_grid(m, j.s);
    if (ret)
        return ret;
    j.m = m;
    j.sb = sb;
    j.subs = subs;
    j.max_subs = max_subs;
    decode_job(&j);
    return j.ret;
}

/* Below this size a picture's slices are decoded one after the other: starting threads costs more */
#define THREAD_MIN_BYTES 16384

/* Merge the slices of one access unit (Annex B). Returns the length written to out, or a negative
 * E_* code: then out is unchanged in meaning and the caller keeps the input. flags & 1: decode the
 * merged slice again before accepting it. */
API long hm_merge(void *handle, const uint8_t *in, size_t n, uint8_t *out, size_t cap, int flags)
{
    Merger *m = handle;
    Nal nals[MAX_NALS];
    SliceBuf sb[MAX_NALS];
    int nn, i, nvcl = 0, vcl_idx[MAX_NALS], nsub = 0, ret = 0;
    const SPS *s = NULL;
    const PPS *p = NULL;
    size_t o = 0;

    memset(sb, 0, sizeof(sb));
    nn = split_nals(in, n, nals, MAX_NALS);
    if (nn < 0)
        return -E_FEW;
    for (i = 0; i < nn; i++) {
        if (nals[i].type == 33 || nals[i].type == 34)
            param_set(m, nals[i].type, nals[i].p + 2, nals[i].n - 2);
        else if (nals[i].type >= 0 && nals[i].type < 32)
            vcl_idx[nvcl++] = i;
    }
    if (nvcl < 2)
        return -E_FEW;

    for (i = 0; i < nvcl && !ret; i++) {
        const Nal *nal = &nals[vcl_idx[i]];
        const SPS *s1;
        const PPS *p1;
        if (nal->n < 4 || nal->p[0] != nals[vcl_idx[0]].p[0] || nal->p[1] != nals[vcl_idx[0]].p[1]) {
            ret = -E_MISMATCH;
            break;
        }
        ret = prepare_vcl(m, nal, &sb[i], &s1, &p1);
        if (ret)
            break;
        if (i == 0) {
            s = s1;
            p = p1;
            if (!sb[0].sh.first || sb[0].sh.addr != 0) {
                ret = -E_LAYOUT;
                break;
            }
        } else {
            const Slice *a = &sb[0].sh, *b = &sb[i].sh;
            if (p1 != p || b->first || b->no_output != a->no_output) {
                ret = -E_MISMATCH;
                break;
            }
            if (a->ep_pos - a->body_start != b->ep_pos - b->body_start ||
                !bits_equal(sb[0].rbsp, a->body_start, sb[i].rbsp, b->body_start, a->ep_pos - a->body_start) ||
                a->ext_end - a->ext_start != b->ext_end - b->ext_start ||
                !bits_equal(sb[0].rbsp, a->ext_start, sb[i].rbsp, b->ext_start, a->ext_end - a->ext_start)) {
                ret = -E_MISMATCH;
                break;
            }
        }
    }
    /* decode the slices, in parallel for big pictures; each gets room for as many substreams as there
     * are tiles */
    if (!ret) {
        int ntiles = p->ncols * p->nrows, started[MAX_NALS];
        pthread_t th[MAX_NALS];
        Job jobs[MAX_NALS];
        int threads = n >= THREAD_MIN_BYTES;
        if (nvcl * ntiles > MAX_SUBS)
            ret = -E_LAYOUT;
        else
            ret = ensure_grid(m, s);
        for (i = 0; i < nvcl && !ret; i++) {
            Job *j = &jobs[i];
            j->m = m;
            j->s = s;
            j->p = p;
            j->sb = &sb[i];
            j->subs = m->subs + i * ntiles;
            j->max_subs = ntiles;
            j->ret = 0;
            sb[i].first_sub = i * ntiles;
            started[i] = threads && i > 0 && pthread_create(&th[i], NULL, decode_job, j) == 0;
        }
        if (!ret) {
            decode_job(&jobs[0]);
            for (i = 1; i < nvcl; i++) {
                if (started[i])
                    pthread_join(th[i], NULL);
                else
                    decode_job(&jobs[i]);
            }
            for (i = 0; i < nvcl && !ret; i++)
                ret = jobs[i].ret;
        }
        for (i = 0; i < nvcl && !ret; i++) {
            nsub += sb[i].nsub;
            if (i > 0 && m->tiles.rs2ts[sb[i].sh.addr] != sb[i - 1].ts_end)
                ret = -E_LAYOUT;
        }
    }
    if (!ret && sb[nvcl - 1].ts_end != s->ctb_w * s->ctb_h)
        ret = -E_LAYOUT;
    /* deblocking/SAO across the old slice edges: loop_filter_across_tiles_enabled_flag decides after the
     * merge, slice_loop_filter_across_slices_enabled_flag && the tiles flag decided before */
    if (!ret && p->lf_tiles && !sb[0].sh.lf_across)
        ret = -E_FILTER;

    if (!ret) {
        /* the merged slice: header, then each substream escaped on its own (each ends with a non-zero
         * byte, so emulation prevention never spans two of them) */
        size_t total = 0;
        size_t *esc = malloc(sizeof(size_t) * (size_t)nsub);
        int k = 0, j;
        if (!esc) {
            ret = -E_NOMEM;
        } else {
            uint8_t **data = calloc((size_t)nsub, sizeof(uint8_t *));
            size_t *lens = calloc((size_t)nsub, sizeof(size_t));
            if (!data || !lens)
                ret = -E_NOMEM;
            for (i = 0; i < nvcl && !ret; i++)
                for (j = 0; j < sb[i].nsub && !ret; j++, k++) {
                    Sub *u = &m->subs[sb[i].first_sub + j];
                    size_t len = u->end - u->start;
                    data[k] = malloc(len + 8);
                    if (!data[k]) {
                        ret = -E_NOMEM;
                        break;
                    }
                    memcpy(data[k], sb[i].rbsp + u->start, len);
                    memset(data[k] + len, 0, 8);
                    lens[k] = len;
                    if (i < nvcl - 1 && j == sb[i].nsub - 1) {
                        lens[k] = rewrite_tail(data[k], len, u->stop - (uint64_t)u->start * 8, u->range,
                                               u->offset);
                        if (!lens[k])
                            ret = -E_TAIL;
                    }
                    if (!ret) {
                        esc[k] = escaped_size(data[k], lens[k]);
                        total += esc[k];
                    }
                }
            if (!ret) {
                /* header */
                const Slice *a = &sb[0].sh;
                size_t hcap = (a->ext_end + 64 + (size_t)nsub * 33) / 8 + 16, maxoff = 0;
                uint8_t *hdr = malloc(hcap);
                BW w;
                int len_bits = 1;
                if (!hdr) {
                    ret = -E_NOMEM;
                } else {
                    w.b = hdr;
                    w.cap = hcap;
                    w.pos = 0;
                    copy_bits(&w, sb[0].rbsp, 0, a->ep_pos);
                    for (k = 0; k < nsub - 1; k++)
                        if (esc[k] - 1 > maxoff)
                            maxoff = esc[k] - 1;
                    while (len_bits < 32 && (maxoff >> len_bits))
                        len_bits++;
                    wue(&w, (uint32_t)(nsub - 1));
                    if (nsub > 1) {
                        wue(&w, (uint32_t)(len_bits - 1));
                        for (k = 0; k < nsub - 1; k++)
                            wbits(&w, (uint32_t)(esc[k] - 1), len_bits);
                    }
                    copy_bits(&w, sb[0].rbsp, a->ext_start, a->ext_end);
                    wbits(&w, 1, 1);
                    while (w.pos & 7)
                        wbits(&w, 0, 1);
                    /* write: non-VCL units in place, the merged slice at the first slice */
                    for (i = 0; i < nn && !ret; i++) {
                        const Nal *nal = &nals[i];
                        if (nal->type >= 0 && nal->type < 32 && i != vcl_idx[0])
                            continue;
                        if (o + 4 + nal->n > cap) {
                            ret = -E_SPACE;
                            break;
                        }
                        memcpy(out + o, "\0\0\0\1", 4);
                        o += 4;
                        if (i != vcl_idx[0]) {
                            memcpy(out + o, nal->p, nal->n);
                            o += nal->n;
                            continue;
                        }
                        {
                            size_t got, begin = o;
                            out[o++] = nal->p[0];
                            out[o++] = nal->p[1];
                            got = escape(out + o, cap - o, hdr, w.pos >> 3);
                            if (!got) {
                                ret = -E_SPACE;
                                break;
                            }
                            o += got;
                            for (k = 0; k < nsub; k++) {
                                got = escape(out + o, cap - o, data[k], lens[k]);
                                if (!got || got != esc[k]) {
                                    ret = -E_SPACE;
                                    break;
                                }
                                o += got;
                            }
                            if (!ret && (flags & 1)) {
                                /* decode the merged slice again */
                                Nal mn;
                                SliceBuf vb;
                                Sub *vsubs = malloc(sizeof(Sub) * (size_t)(nsub + 1));
                                memset(&vb, 0, sizeof(vb));
                                mn.p = out + begin;
                                mn.n = o - begin;
                                mn.type = nal->type;
                                if (!vsubs ||
                                    parse_vcl(m, &mn, &vb, vsubs, nsub + 1) || vb.nsub != nsub ||
                                    vb.ts_end != s->ctb_w * s->ctb_h)
                                    ret = -E_VERIFY;
                                free(vsubs);
                                free(vb.rbsp);
                            }
                        }
                    }
                    free(hdr);
                }
            }
            if (data)
                for (k = 0; k < nsub; k++)
                    free(data[k]);
            free(data);
            free(lens);
        }
        free(esc);
    }
    for (i = 0; i < nvcl; i++)
        free(sb[i].rbsp);
    if (ret < 0) {
        if (-ret < 16)
            m->stats[-ret]++;
        return ret;
    }
    m->stats[0]++;
    return (long)o;
}

/* counters: [0] merged pictures, [code] failures by E_* code */
API long hm_stat(void *handle, int i)
{
    Merger *m = handle;
    return (i >= 0 && i < 16) ? m->stats[i] : 0;
}
