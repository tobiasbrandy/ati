from typing import Callable, Tuple

import dearpygui.dearpygui as dpg
import numpy as np

from image_utils import image_to_rgba_array, load_image, valid_image_formats, Image, save_image, get_extension, \
    create_square_image, create_circle_image, CIRCLE_IMAGE_NAME, SQUARE_IMAGE_NAME
import images_repo as img_repo
from interface_utils import render_error
from metadata_repo import set_metadata_file
from transformations import TRANSFORMATIONS

# General Items
PRIMARY_WINDOW: str = 'primary'
IMAGES_MENU: str = 'images_menu'

# Registries
TEXTURE_REGISTRY: str = 'texture_registry'

# Dialog Tags
LOAD_IMAGE_DIALOG: str = 'load_image_dialog'
SAVE_IMAGE_DIALOG: str = 'save_image_dialog'
LOAD_METADATA_DIALOG: str = 'load_metadata_dialog'

CENTER_POS: Tuple[int, int] = (750, 350)
MIN_IMAGE_SIZE: Tuple[int, int] = (200, 200)

# Creates window only if it doesn't exist
@render_error
def render_image_window(image_name: str):
    if dpg.does_item_exist(f'image_{image_name}'):
        dpg.focus_item(f'image_{image_name}')
    else:
        with dpg.window(label=image_name, pos=CENTER_POS, min_size=MIN_IMAGE_SIZE, no_resize=True, on_close=lambda: dpg.delete_item(window)) as window:
            with dpg.menu_bar():
                dpg.add_menu_item(label='Save', user_data=image_name, callback=lambda s, ad, ud: trigger_save_image_dialog(ud))
                with dpg.menu(label='Transform'):
                    for name, tr in TRANSFORMATIONS.items():
                        dpg.add_menu_item(label=name.capitalize(), user_data=image_name, callback=lambda s, ad, ud: tr(ud))
            
            dpg.set_item_user_data(window, {})

            image = dpg.add_image(image_name, tag=f'image_{image_name}')

            img_meta: Image = img_repo.get_image(image_name)

            dpg.add_text(f'Height: {img_meta.height}  Width: {img_meta.width}')
            dpg.add_text('', tag=f'image_{image_name}_pointer')
            dpg.add_text('', tag=f'image_{image_name}_region')

            def get_pixel_pos() -> Tuple[int, int]:
                mouse_pos  = dpg.get_mouse_pos(local=False)
                window_pos = dpg.get_item_pos(window)
                img_pos    = dpg.get_item_pos(image)
                return int(mouse_pos[0] - window_pos[0] - img_pos[0]), int(mouse_pos[1] - window_pos[1] - img_pos[1])

            def mouse_move_handler():
                if dpg.is_item_hovered(image) and dpg.is_item_focused(window):
                    pixel = get_pixel_pos()

                    usr_data = dpg.get_item_user_data(window)

                    if 'init_draw' in usr_data:
                        if dpg.does_item_exist(f'image_{image_name}_selection'):
                            dpg.delete_item(f'image_{image_name}_selection')
                    
                        dpg.draw_rectangle(usr_data['init_draw'], pixel, parent=window, tag=f'image_{image_name}_selection')

                    dpg.show_item(f'image_{image_name}_pointer')
                    dpg.set_value(f'image_{image_name}_pointer', f"Pixel: {get_pixel_pos()}  Value: {img_meta.data[int(pixel[1])][int(pixel[0])]}")
                else:
                    if dpg.does_item_exist(f'image_{image_name}_selection'):
                        dpg.delete_item(f'image_{image_name}_selection')
                        
                    dpg.hide_item(f'image_{image_name}_pointer')

            def mouse_down_handler():
                usr_data = dpg.get_item_user_data(window)

                usr_data['init_draw'] = usr_data.get('init_draw', get_pixel_pos())

                dpg.set_item_user_data(window, usr_data)

            def mouse_release_handler():
                usr_data = dpg.get_item_user_data(window)
                
                pixel_pos = get_pixel_pos()
                init_draw = usr_data['init_draw']
                region_size = (pixel_pos[0] - init_draw[0] + 1, pixel_pos[1] - init_draw[1] + 1)

                x = (int(min(pixel_pos[0], init_draw[0])), int(max(pixel_pos[0], init_draw[0])))
                y = (int(min(pixel_pos[1], init_draw[1])), int(max(pixel_pos[1], init_draw[1])))
                
                if img_meta.channels > 1:
                    mean = np.mean(np.array(img_meta.data[y[0]:y[1], x[0]:x[1]]).reshape((-1, 3)), axis=0)
                else:
                    mean = np.mean(img_meta.data[y[0]:y[1], x[0]:x[1]])

                dpg.set_value(f'image_{image_name}_region', f"#Pixel: {np.prod(np.abs(region_size))}  Avg: {np.around(mean, 2)}")
                
                usr_data.pop('init_draw', None)

                dpg.set_item_user_data(window, usr_data)

            with dpg.handler_registry():
                dpg.add_mouse_move_handler(callback=mouse_move_handler)
                dpg.add_mouse_down_handler(callback=mouse_down_handler)
                dpg.add_mouse_release_handler(callback=mouse_release_handler)

            
def register_image(image: Image) -> None:
    image_vector = image_to_rgba_array(image)
    dpg.add_static_texture(image.width, image.height, image_vector, tag=image.name, parent=TEXTURE_REGISTRY)
    dpg.add_menu_item(label=image.name, parent=IMAGES_MENU, user_data=image.name, callback=lambda s, ad, ud: render_image_window(ud))

@render_error
def load_image_handler(app_data):
    path = app_data['file_path_name']
    image_name = Image.name_from_path(path)

    if not img_repo.contains_image(image_name):
        image = load_image(path)
        img_repo.persist_image(image)
        register_image(image)

    render_image_window(image_name)

def save_image_handler(app_data, image_name: str) -> None:
    image = img_repo.get_image(image_name)
    dir_path = app_data['file_path_name']
    save_image(image, dir_path)

def load_metadata_handler(app_data):
    path = app_data['file_path_name']
    if not get_extension(path) == '.tsv':
        raise ValueError('Metadata file must be a tsv')
    set_metadata_file(path)

# Generic function to create images
def create_image(name: str, supplier: Callable[[], Image]) -> None:
    if not img_repo.contains_image(name):
        image = supplier()
        img_repo.persist_image(image)
        register_image(image)

    render_image_window(name)

def create_circle_handler():
    create_image(CIRCLE_IMAGE_NAME, create_circle_image)

def create_square_handler():
    create_image(SQUARE_IMAGE_NAME, create_square_image)

@render_error
def trigger_save_image_dialog(image_name: str) -> None:
    dpg.set_item_label(SAVE_IMAGE_DIALOG, f'Choose where to save {image_name}...')
    dpg.set_item_user_data(SAVE_IMAGE_DIALOG, image_name)
    dpg.show_item(SAVE_IMAGE_DIALOG)

def build_save_image_dialog() -> None:
    dpg.add_file_dialog(tag=SAVE_IMAGE_DIALOG, default_path='images', directory_selector=True, show=False, modal=True, width=1024, height=512, callback=lambda s, ad, ud: save_image_handler(ad, ud))

def build_load_image_dialog() -> None:
    with dpg.file_dialog(label='Choose file to load...', tag=LOAD_IMAGE_DIALOG, default_path='images', directory_selector=False, show=False, modal=True, width=1024, height=512, callback=lambda s, ad: load_image_handler(ad)):
        dpg.add_file_extension(f'Image{{{",".join(valid_image_formats())}}}')

def build_load_metadata_dialog() -> None:
    with dpg.file_dialog(label='Choose metadata file to load...', tag=LOAD_METADATA_DIALOG, default_path='images', directory_selector=False, show=False, modal=True, width=1024, height=512, callback=lambda s, ad: load_metadata_handler(ad)):
        dpg.add_file_extension('.tsv')