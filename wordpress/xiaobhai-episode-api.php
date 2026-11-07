<?php
/**
 * Plugin Name: Xiao Bhai Episode API
 * Description: Token-protected endpoint so the Xiao Bhai Telegram bot can create episode posts (AnimeStream theme): series, category, video embeds and featured image.
 * Version:     1.1.0
 *
 * INSTALL (no wp-admin needed):
 *   Hostinger hPanel -> File Manager -> public_html/wp-content/ -> create the folder "mu-plugins"
 *   (if it is missing) -> upload this file into it. "Must-use" plugins load by themselves.
 *
 * The secret below must be the SAME as WP_BOT_TOKEN in the bot's environment variables.
 */

if (!defined('ABSPATH')) {
    exit;
}

if (!defined('XB_BOT_TOKEN')) {
    define('XB_BOT_TOKEN', '60d24aa9ff1fed891107c37f060fb5728c4e77b45ea1e862');
}

final class XB_Episode_API
{
    const NS      = 'xiaobhai/v1';
    const OPT_MAP = 'xb_field_map';

    public static function init()
    {
        add_action('rest_api_init', array(__CLASS__, 'routes'));
    }

    public static function routes()
    {
        $auth = array(__CLASS__, 'authorize');

        register_rest_route(self::NS, '/ping', array(
            'methods' => 'GET', 'callback' => array(__CLASS__, 'ping'), 'permission_callback' => $auth,
        ));
        register_rest_route(self::NS, '/series', array(
            'methods' => 'GET', 'callback' => array(__CLASS__, 'series'), 'permission_callback' => $auth,
        ));
        register_rest_route(self::NS, '/categories', array(
            'methods' => 'GET', 'callback' => array(__CLASS__, 'categories'), 'permission_callback' => $auth,
        ));
        register_rest_route(self::NS, '/calibrate', array(
            'methods' => 'POST', 'callback' => array(__CLASS__, 'calibrate'), 'permission_callback' => $auth,
        ));
        register_rest_route(self::NS, '/episode', array(
            'methods' => 'POST', 'callback' => array(__CLASS__, 'create_episode'), 'permission_callback' => $auth,
        ));
    }

    // ------------------------------------------------------------------ security
    public static function authorize($request)
    {
        $ip = 'unknown';
        if (!empty($_SERVER['HTTP_CF_CONNECTING_IP'])) {
            $ip = (string) $_SERVER['HTTP_CF_CONNECTING_IP'];
        } elseif (!empty($_SERVER['REMOTE_ADDR'])) {
            $ip = (string) $_SERVER['REMOTE_ADDR'];
        }
        $key   = 'xb_fail_' . md5($ip);
        $fails = (int) get_transient($key);
        if ($fails >= 10) {
            return new WP_Error('xb_locked', 'Too many wrong tokens. Try again in 10 minutes.', array('status' => 429));
        }

        $secret = (string) XB_BOT_TOKEN;
        $given  = $request->get_header('x-xb-token');
        if (!$given) {
            $given = $request->get_param('token'); // fallback if the host strips custom headers
        }

        if (strlen($secret) >= 20 && is_string($given) && hash_equals($secret, $given)) {
            do_action('litespeed_control_set_nocache', 'xiaobhai api'); // never cache API answers
            return true;
        }

        set_transient($key, $fails + 1, 10 * MINUTE_IN_SECONDS);
        return new WP_Error('xb_forbidden', 'Invalid token.', array('status' => 401));
    }

    // ------------------------------------------------------------------ helpers
    private static function out($data)
    {
        $res = rest_ensure_response($data);
        $res->header('Cache-Control', 'no-store');
        return $res;
    }

    private static function map()
    {
        $map = get_option(self::OPT_MAP, array());
        return is_array($map) ? $map : array();
    }

    private static function clip($text, $len)
    {
        $text = (string) $text;
        return function_exists('mb_substr') ? mb_substr($text, 0, $len) : substr($text, 0, $len);
    }

    private static function series_types($map)
    {
        if (!empty($map['series_type']) && post_type_exists($map['series_type'])) {
            return array($map['series_type']);
        }
        $types = array();
        foreach (array('series', 'anime') as $pt) {
            if (post_type_exists($pt)) {
                $types[] = $pt;
            }
        }
        return $types;
    }

    private static function author_id()
    {
        if (defined('XB_POST_AUTHOR')) {
            return (int) XB_POST_AUTHOR;
        }
        $ids = get_users(array('role' => 'administrator', 'number' => 1, 'orderby' => 'ID', 'order' => 'ASC', 'fields' => 'ID'));
        return !empty($ids) ? (int) $ids[0] : 1;
    }


    // ------------------------------------------------------------------ embeds
    private static function has_iframe($v)
    {
        return is_string($v) && stripos($v, '<iframe') !== false;
    }

    // Which shape does the theme use to keep video embeds? Learned from the value of ONE existing post.
    private static function embed_shape($val)
    {
        if (self::has_iframe($val)) {
            return array('embed_shape' => 'string');
        }
        if (!is_array($val) || count($val) === 0) {
            return null;
        }
        $first = reset($val);
        if (is_array($first)) { // list of records: [ ['name' => 'Host', 'embed' => '<iframe..'], ... ]
            $code_field = null;
            foreach ($first as $f => $v) {
                if (self::has_iframe($v)) {
                    $code_field = $f;
                    break;
                }
            }
            if ($code_field === null) {
                return null;
            }
            $host_field = null;
            foreach ($first as $f => $v) {
                if ($f !== $code_field && is_string($v) && preg_match('/host|name|server|title|label|provider|mirror/i', (string) $f)) {
                    $host_field = $f;
                    break;
                }
            }
            if ($host_field === null) {
                foreach ($first as $f => $v) {
                    if ($f !== $code_field && is_string($v)) {
                        $host_field = $f;
                        break;
                    }
                }
            }
            $template = array();
            foreach ($first as $f => $v) {
                $template[$f] = is_array($v) ? array() : '';
            }
            return array(
                'embed_shape'      => 'list',
                'embed_code_field' => $code_field,
                'embed_host_field' => $host_field,
                'embed_template'   => $template,
            );
        }
        $any = false; // map: [ 'Server 1' => '<iframe..', ... ]
        foreach ($val as $v) {
            if (!is_string($v)) {
                return null;
            }
            if (self::has_iframe($v)) {
                $any = true;
            }
        }
        return $any ? array('embed_shape' => 'assoc') : null;
    }

    private static function clean_embeds($raw)
    {
        $out = array();
        if (!is_array($raw)) {
            return $out;
        }
        foreach ($raw as $e) {
            if (!is_array($e)) {
                continue;
            }
            $host = isset($e['host']) ? trim(wp_strip_all_tags((string) $e['host'])) : '';
            $code = isset($e['code']) ? trim((string) $e['code']) : '';
            if ($code === '' || strlen($code) > 5000 || !self::has_iframe($code)) {
                continue;
            }
            $out[] = array('host' => self::clip($host !== '' ? $host : 'Server', 60), 'code' => $code);
            if (count($out) >= 10) {
                break;
            }
        }
        return $out;
    }

    // Decide where the embeds go. Nothing is written here: returns meta to write after the insert + text for the post body.
    private static function prepare_embeds($embeds, $map)
    {
        $res = array('meta' => array(), 'content' => '', 'saved' => 0, 'where' => 'none', 'notes' => array());
        if (empty($embeds)) {
            return $res;
        }
        $left = $embeds;
        if (!empty($map['embed_key']) && !empty($map['embed_shape'])) {
            $key   = $map['embed_key'];
            $shape = $map['embed_shape'];
            if ($shape === 'list' && !empty($map['embed_code_field'])) {
                $items = array();
                foreach ($embeds as $e) {
                    $row = (isset($map['embed_template']) && is_array($map['embed_template'])) ? $map['embed_template'] : array();
                    $row[$map['embed_code_field']] = $e['code'];
                    if (!empty($map['embed_host_field'])) {
                        $row[$map['embed_host_field']] = $e['host'];
                    }
                    $items[] = $row;
                }
                $res['meta'][$key] = $items;
                $res['saved']      = count($items);
                $res['where']      = 'meta';
                $left              = array();
            } elseif ($shape === 'assoc') {
                $items = array();
                foreach ($embeds as $e) {
                    $k = $e['host'];
                    $n = 2;
                    while (isset($items[$k])) {
                        $k = $e['host'] . ' ' . $n++;
                    }
                    $items[$k] = $e['code'];
                }
                $res['meta'][$key] = $items;
                $res['saved']      = count($items);
                $res['where']      = 'meta';
                $left              = array();
            } elseif ($shape === 'string') {
                $res['meta'][$key] = $embeds[0]['code'];
                $res['saved']      = 1;
                $res['where']      = 'meta';
                $left              = array_slice($embeds, 1);
                if (!empty($left)) {
                    $res['notes'][] = 'The theme keeps one embed in its field, so the other hosts were put in the post text.';
                }
            }
        }
        foreach ($left as $e) {
            $res['content'] .= '<h3>' . esc_html($e['host']) . "</h3>\n" . $e['code'] . "\n\n";
            $res['saved']++;
            if ($res['where'] === 'none') {
                $res['where'] = 'content';
            }
        }
        return $res;
    }

    // ------------------------------------------------------------------ featured image (only from TMDB)
    private static function attach_image($post_id, $url, $title)
    {
        $parts = wp_parse_url($url);
        if (empty($parts['host']) || empty($parts['scheme']) || $parts['scheme'] !== 'https' || strtolower($parts['host']) !== 'image.tmdb.org') {
            return new WP_Error('xb_bad_image', 'Only image.tmdb.org pictures are accepted.');
        }
        require_once ABSPATH . 'wp-admin/includes/file.php';
        require_once ABSPATH . 'wp-admin/includes/media.php';
        require_once ABSPATH . 'wp-admin/includes/image.php';

        $tmp = download_url($url, 45);
        if (is_wp_error($tmp)) {
            return $tmp;
        }
        $ext = strtolower(pathinfo(isset($parts['path']) ? $parts['path'] : '', PATHINFO_EXTENSION));
        if (!in_array($ext, array('jpg', 'jpeg', 'png', 'webp'), true)) {
            $ext = 'jpg';
        }
        $base = sanitize_title($title);
        $file = array('name' => ($base !== '' ? $base : 'poster') . '.' . $ext, 'tmp_name' => $tmp);
        $att  = media_handle_sideload($file, $post_id, $title);
        if (is_wp_error($att)) {
            @unlink($tmp);
            return $att;
        }
        set_post_thumbnail($post_id, $att);
        return (int) $att;
    }

    // ------------------------------------------------------------------ routes
    public static function ping($request)
    {
        $theme = wp_get_theme();
        return self::out(array(
            'ok'     => true,
            'plugin' => '1.1.0',
            'wp'     => get_bloginfo('version'),
            'php'    => PHP_VERSION,
            'theme'  => $theme ? $theme->get('Name') : '',
            'map'    => self::map(),
        ));
    }

    public static function series($request)
    {
        $map   = self::map();
        $types = self::series_types($map);
        if (empty($types)) {
            return new WP_Error('xb_no_series_type', 'Series post type unknown. Run /wpcheck in the bot first.', array('status' => 409));
        }
        $q    = sanitize_text_field((string) $request->get_param('search'));
        $args = array(
            'post_type'      => $types,
            'post_status'    => array('publish', 'draft', 'private', 'pending'),
            'posts_per_page' => 8,
            'no_found_rows'  => true,
        );
        if ($q !== '') {
            $args['s'] = $q;
        } else {
            $args['orderby'] = 'date';
            $args['order']   = 'DESC';
        }
        $items = array();
        foreach (get_posts($args) as $p) {
            $items[] = array('id' => (int) $p->ID, 'title' => wp_specialchars_decode($p->post_title), 'status' => $p->post_status);
        }
        return self::out(array('ok' => true, 'items' => $items));
    }

    public static function categories($request)
    {
        $q    = sanitize_text_field((string) $request->get_param('search'));
        $args = array('taxonomy' => 'category', 'hide_empty' => false, 'number' => 10, 'orderby' => 'name', 'order' => 'ASC');
        if ($q !== '') {
            $args['search'] = $q;
        }
        $terms = get_terms($args);
        $items = array();
        if (!is_wp_error($terms)) {
            foreach ($terms as $t) {
                $items[] = array('id' => (int) $t->term_id, 'title' => wp_specialchars_decode($t->name), 'count' => (int) $t->count);
            }
        }
        return self::out(array('ok' => true, 'items' => $items));
    }

    /**
     * Learns which custom-field names AnimeStream uses by looking at ONE existing episode post
     * (episode number, series, "Subbed?"). Nothing is guessed: values are matched against what the post shows.
     */
    public static function calibrate($request)
    {
        $slug = sanitize_title((string) $request->get_param('slug'));
        $ep   = (string) (int) $request->get_param('episode');
        $post = $slug !== '' ? get_page_by_path($slug, OBJECT, 'post') : null;
        if (!$post) {
            return new WP_Error('xb_no_reference', 'Reference episode post not found (slug: ' . $slug . ').', array('status' => 404));
        }

        $skip_types = array('post', 'page', 'attachment', 'revision', 'nav_menu_item');
        $ep_keys    = array();
        $series_c   = array();
        $sub_key    = null;
        $sub_value  = null;
        $dump       = array();

        foreach (get_post_meta($post->ID) as $key => $values) {
            if (!is_array($values) || count($values) === 0) {
                continue;
            }
            if (strpos($key, '_') === 0 || strpos($key, 'rank_math') === 0) {
                continue; // WordPress / SEO internals
            }
            $val = maybe_unserialize($values[0]);
            if (is_array($val) || is_object($val)) {
                $dump[$key] = '[list]';
                continue;
            }
            $val         = (string) $val;
            $dump[$key]  = self::clip($val, 60);
            $lower       = strtolower(trim($val));

            if ($val === $ep) {
                $ep_keys[] = $key;
                continue;
            }
            if ($sub_key === null && in_array($lower, array('sub', 'dub', 'none', 'subbed', 'dubbed'), true)) {
                $sub_key   = $key;
                $sub_value = $val;
                continue;
            }
            if (ctype_digit($val) && (int) $val > 0) {
                $pt = get_post_type((int) $val);
                if ($pt && !in_array($pt, $skip_types, true)) {
                    $series_c[$key] = $pt;
                }
            }
        }

        $ep_key = self::pick($ep_keys, 'ep');
        $s_key  = null;
        if (count($series_c) > 0) {
            $s_key = self::pick(array_keys($series_c), 'seri');
        }

        // Optional: where does the theme keep the video embed(s)? (an episode post that has one is needed for this)
        $embed = null;
        $best  = 0;
        foreach (get_post_meta($post->ID) as $key => $values) {
            if (!is_array($values) || count($values) === 0) {
                continue;
            }
            if (in_array($key, array('_edit_lock', '_edit_last', '_thumbnail_id'), true) || strpos($key, '_wp_') === 0
                || strpos($key, '_oembed') === 0 || strpos($key, 'rank_math') === 0 || strpos($key, '_rank_math') === 0) {
                continue;
            }
            $shape = self::embed_shape(maybe_unserialize($values[0]));
            if (!$shape) {
                continue;
            }
            $weight = preg_match('/embed|mirror|player|video|stream/i', $key) ? 2 : 1;
            if ($weight > $best) {
                $best  = $weight;
                $embed = array('embed_key' => $key) + $shape;
            }
        }

        $missing = array();
        if (!$ep_key) {
            $missing[] = 'episode number field';
        }
        if (!$s_key) {
            $missing[] = 'series field';
        }
        if (!$sub_key) {
            $missing[] = 'Subbed? field';
        }
        if (!empty($missing)) {
            return self::out(array('ok' => false, 'missing' => $missing, 'post_id' => (int) $post->ID, 'meta' => $dump));
        }

        $map = array(
            'episode_key' => $ep_key,
            'series_key'  => $s_key,
            'series_type' => $series_c[$s_key],
            'sub_key'     => $sub_key,
            'sub_value'   => $sub_value,
        );
        if ($embed) {
            $map = $map + $embed;
        }
        update_option(self::OPT_MAP, $map, false);
        return self::out(array('ok' => true, 'map' => $map, 'post_id' => (int) $post->ID));
    }

    // best candidate: the one whose name contains $hint; otherwise the only one; otherwise none
    private static function pick($keys, $hint)
    {
        if (count($keys) === 0) {
            return null;
        }
        foreach ($keys as $k) {
            if (stripos($k, $hint) !== false) {
                return $k;
            }
        }
        return count($keys) === 1 ? $keys[0] : null;
    }

    public static function create_episode($request)
    {
        $map = self::map();
        if (empty($map['episode_key']) || empty($map['series_key']) || empty($map['sub_key'])) {
            return new WP_Error('xb_not_calibrated', 'Field names not learned yet. Run /wpcheck in the bot first.', array('status' => 409));
        }

        $title     = trim(wp_strip_all_tags((string) $request->get_param('title')));
        $episode   = (int) $request->get_param('episode');
        $series_id = (int) $request->get_param('series_id');
        $status    = ((string) $request->get_param('status') === 'publish') ? 'publish' : 'draft';
        $embeds    = self::clean_embeds($request->get_param('embeds'));
        $image_url = trim((string) $request->get_param('featured_image_url'));
        $image_ttl = trim(wp_strip_all_tags((string) $request->get_param('image_title')));
        $cats      = array();
        foreach ((array) $request->get_param('category_ids') as $c) {
            if ((int) $c > 0) {
                $cats[] = (int) $c;
            }
        }

        if ($title === '' || strlen($title) > 250) {
            return new WP_Error('xb_bad_title', 'Title is empty or too long.', array('status' => 400));
        }
        if ($episode < 1) {
            return new WP_Error('xb_bad_episode', 'Episode number is missing.', array('status' => 400));
        }
        if ($series_id < 1 || !get_post($series_id)) {
            return new WP_Error('xb_bad_series', 'That series does not exist on the website.', array('status' => 400));
        }

        // already there? (same series + same episode, any status) -> do not create a second one
        $dupe = get_posts(array(
            'post_type'      => 'post',
            'post_status'    => 'any',
            'posts_per_page' => 1,
            'fields'         => 'ids',
            'no_found_rows'  => true,
            'meta_query'     => array(
                'relation' => 'AND',
                array('key' => $map['series_key'], 'value' => (string) $series_id),
                array('key' => $map['episode_key'], 'value' => (string) $episode),
            ),
        ));
        if (!empty($dupe)) {
            $id = (int) $dupe[0];
            return self::out(array(
                'ok' => true, 'api' => 2, 'exists' => true, 'id' => $id, 'status' => get_post_status($id),
                'edit_url' => admin_url('post.php?post=' . $id . '&action=edit'), 'link' => get_permalink($id),
            ));
        }

        // Act as the site's administrator for this request, so WordPress does not strip the <iframe> from the post text.
        wp_set_current_user(self::author_id());

        $prep = self::prepare_embeds($embeds, $map);
        $args = array(
            'post_type'    => 'post',
            'post_status'  => $status,
            'post_title'   => $title,
            'post_content' => wp_slash($prep['content']),
            'post_author'  => self::author_id(),
        );
        if (!empty($cats)) {
            $args['post_category'] = $cats;
        }
        $id = wp_insert_post($args, true);
        if (is_wp_error($id)) {
            return $id;
        }

        // Custom fields AFTER the insert, so nothing the theme does on save_post can overwrite them.
        update_post_meta($id, $map['episode_key'], (string) $episode);
        update_post_meta($id, $map['series_key'], (string) $series_id);
        update_post_meta($id, $map['sub_key'], $map['sub_value']);
        foreach ($prep['meta'] as $k => $v) {
            update_post_meta($id, $k, wp_slash($v));
        }

        $image = '';
        $image_error = '';
        if ($image_url !== '') {
            $att = self::attach_image($id, $image_url, $image_ttl !== '' ? $image_ttl : $title);
            if (is_wp_error($att)) {
                $image       = 'failed';
                $image_error = $att->get_error_message();
            } else {
                $image = 'ok';
            }
        }

        return self::out(array(
            'ok' => true, 'api' => 2, 'exists' => false, 'id' => (int) $id, 'status' => get_post_status($id),
            'edit_url' => admin_url('post.php?post=' . $id . '&action=edit'), 'link' => get_permalink($id),
            'embeds_saved' => (int) $prep['saved'], 'embeds_where' => $prep['where'], 'notes' => $prep['notes'],
            'image' => $image, 'image_error' => $image_error,
        ));
    }
}

XB_Episode_API::init();
